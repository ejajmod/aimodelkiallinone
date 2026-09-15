package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func payload(t *testing.T, size int) []byte {
	t.Helper()
	data := make([]byte, size)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	return data
}

func rangeServer(data []byte, before func(w http.ResponseWriter, r *http.Request) bool) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if before != nil && !before(w, r) {
			return
		}
		http.ServeContent(w, r, "model.bin", time.Time{}, bytes.NewReader(data))
	}))
}

func testJob(t *testing.T, target string, size, segment int64, connections int) *job {
	t.Helper()
	j := newJob(target, http.Header{}, filepath.Join(t.TempDir(), "model.bin.part"), size, segment, connections)
	j.retryBase = time.Millisecond
	j.progress = io.Discard
	return j
}

func TestDownloadsEverySegmentInParallel(t *testing.T) {
	data := payload(t, 3<<20+123)
	var active, peak atomic.Int32
	server := rangeServer(data, func(http.ResponseWriter, *http.Request) bool {
		now := active.Add(1)
		defer active.Add(-1)
		for {
			old := peak.Load()
			if now <= old || peak.CompareAndSwap(old, now) {
				break
			}
		}
		time.Sleep(5 * time.Millisecond)
		return true
	})
	defer server.Close()
	j := testJob(t, server.URL, int64(len(data)), 256<<10, 8)

	if code := j.run(context.Background()); code != exitOK {
		t.Fatalf("exit %d", code)
	}

	written, _ := os.ReadFile(j.out)
	if !bytes.Equal(written, data) {
		t.Fatal("downloaded bytes differ from the source")
	}
	if peak.Load() < 2 {
		t.Fatalf("expected parallel requests, peak %d", peak.Load())
	}
	var state metadata
	raw, _ := os.ReadFile(j.metadataPath())
	if err := json.Unmarshal(raw, &state); err != nil || len(state.Completed) != 13 {
		t.Fatalf("unexpected state %s (%v)", raw, err)
	}
}

func TestResumesOnlyMissingSegments(t *testing.T) {
	data := payload(t, 4<<20)
	segment := int64(1 << 20)
	var mu sync.Mutex
	var ranges []string
	server := rangeServer(data, func(_ http.ResponseWriter, r *http.Request) bool {
		mu.Lock()
		ranges = append(ranges, r.Header.Get("Range"))
		mu.Unlock()
		return true
	})
	defer server.Close()
	j := testJob(t, server.URL, int64(len(data)), segment, 4)
	// Segments 0 and 1 are already on disk, as the Python downloader would leave them.
	partial := append(append([]byte{}, data[:2*segment]...), make([]byte, 2*segment)...)
	if err := os.WriteFile(j.out, partial, 0o644); err != nil {
		t.Fatal(err)
	}
	state, _ := json.Marshal(metadata{Version: 1, Size: int64(len(data)), SegmentSize: segment, Completed: []int{0, 1}})
	if err := os.WriteFile(j.metadataPath(), state, 0o644); err != nil {
		t.Fatal(err)
	}

	if code := j.run(context.Background()); code != exitOK {
		t.Fatalf("exit %d", code)
	}

	written, _ := os.ReadFile(j.out)
	if !bytes.Equal(written, data) {
		t.Fatal("resumed file differs from the source")
	}
	if strings.Join(ranges, ",") != "bytes=2097152-3145727,bytes=3145728-4194303" &&
		strings.Join(ranges, ",") != "bytes=3145728-4194303,bytes=2097152-3145727" {
		t.Fatalf("unexpected ranges %v", ranges)
	}
}

func TestKeepsTheSegmentLayoutOfAnEarlierRun(t *testing.T) {
	data := payload(t, 4<<20)
	var mu sync.Mutex
	var ranges []string
	server := rangeServer(data, func(_ http.ResponseWriter, r *http.Request) bool {
		mu.Lock()
		ranges = append(ranges, r.Header.Get("Range"))
		mu.Unlock()
		return true
	})
	defer server.Close()
	// This run asks for 512 KiB segments, but 1 MiB segments 0-1 are already on disk.
	j := testJob(t, server.URL, int64(len(data)), 512<<10, 1)
	partial := append(append([]byte{}, data[:2<<20]...), make([]byte, 2<<20)...)
	if err := os.WriteFile(j.out, partial, 0o644); err != nil {
		t.Fatal(err)
	}
	state, _ := json.Marshal(metadata{Version: 1, Size: int64(len(data)), SegmentSize: 1 << 20, Completed: []int{0, 1}})
	if err := os.WriteFile(j.metadataPath(), state, 0o644); err != nil {
		t.Fatal(err)
	}

	if code := j.run(context.Background()); code != exitOK {
		t.Fatalf("exit %d", code)
	}

	written, _ := os.ReadFile(j.out)
	if !bytes.Equal(written, data) {
		t.Fatal("resumed file differs from the source")
	}
	if strings.Join(ranges, ",") != "bytes=2097152-3145727,bytes=3145728-4194303" {
		t.Fatalf("unexpected ranges %v", ranges)
	}
}

func TestServerErrorsAreRetried(t *testing.T) {
	data := payload(t, 1<<20)
	var failures atomic.Int32
	server := rangeServer(data, func(w http.ResponseWriter, _ *http.Request) bool {
		if failures.Add(1) <= 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return false
		}
		return true
	})
	defer server.Close()
	j := testJob(t, server.URL, int64(len(data)), 512<<10, 1)

	if code := j.run(context.Background()); code != exitOK {
		t.Fatalf("exit %d", code)
	}
}

func TestExpiredURLStopsWithItsOwnExitCode(t *testing.T) {
	server := rangeServer(nil, func(w http.ResponseWriter, _ *http.Request) bool {
		w.WriteHeader(http.StatusForbidden)
		return false
	})
	defer server.Close()
	j := testJob(t, server.URL, 4<<20, 1<<20, 4)

	if code := j.run(context.Background()); code != exitForbidden {
		t.Fatalf("exit %d, want %d", code, exitForbidden)
	}
}

func TestIgnoredRangeIsFatal(t *testing.T) {
	data := payload(t, 2<<20)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Write(data)
	}))
	defer server.Close()
	j := testJob(t, server.URL, int64(len(data)), 1<<20, 2)

	if code := j.run(context.Background()); code != exitFailed {
		t.Fatalf("exit %d, want %d", code, exitFailed)
	}
}

func TestInputCarriesURLAndHeaders(t *testing.T) {
	target, headers, err := readInput(strings.NewReader("https://example.invalid/a?X-Amz-Signature=s\nAuthorization: Bearer t\n"))
	if err != nil || target != "https://example.invalid/a?X-Amz-Signature=s" || headers.Get("Authorization") != "Bearer t" {
		t.Fatalf("got %q %v %v", target, headers, err)
	}
	if _, _, err := readInput(strings.NewReader("\n")); err == nil {
		t.Fatal("empty input must be rejected")
	}
}

func TestTransportErrorsDoNotRevealTheURL(t *testing.T) {
	j := testJob(t, "http://127.0.0.1:1/model.bin?X-Amz-Signature=secret", 1<<20, 1<<20, 1)
	_, err := j.fetch(context.Background(), 0, make([]byte, 1024))
	if err == nil || strings.Contains(describe(err), "secret") {
		t.Fatalf("unexpected error text %q", describe(err))
	}
}
