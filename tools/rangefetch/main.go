// Command rangefetch downloads one HTTP(S) object over many keep-alive range
// requests and writes every segment straight to its offset in the target file.
//
// The URL and the request headers are read from stdin (the URL on the first line,
// then "Name: value" lines), so a presigned signature never appears in argv.
// Progress is written to stdout as one JSON object per line. Completed segments are
// recorded in OUT.segments.json, the format used by the launcher's Python
// downloader, so either of them can resume what the other started.
//
// Exit status: 0 done, 1 failed, 2 usage, 3 HTTP 401/403 (the URL needs to be
// renewed), 4 HTTP 404, 130 cancelled by SIGINT or SIGTERM.
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	exitOK        = 0
	exitFailed    = 1
	exitUsage     = 2
	exitForbidden = 3
	exitNotFound  = 4
	exitCancelled = 130

	attempts     = 8
	stallTimeout = 60 * time.Second
)

type metadata struct {
	Version     int   `json:"version"`
	Size        int64 `json:"size"`
	SegmentSize int64 `json:"segment_size"`
	Completed   []int `json:"completed"`
}

type progressLine struct {
	Downloaded int64 `json:"downloaded"`
	Speed      int64 `json:"speed"`
}

// fatalError stops the whole download instead of being retried.
type fatalError struct {
	code    int
	message string
}

func (e *fatalError) Error() string { return e.message }

type job struct {
	target      string
	headers     http.Header
	out         string
	size        int64
	segment     int64
	connections int
	retryBase   time.Duration
	progress    io.Writer
	client      *http.Client

	file        *os.File
	mu          sync.Mutex
	completed   []bool
	lastSave    time.Time
	transferred atomic.Int64
}

func main() {
	out := flag.String("out", "", "target file")
	size := flag.Int64("size", 0, "number of bytes to download from the start of the object")
	connections := flag.Int("connections", 64, "parallel keep-alive connections")
	segmentMB := flag.Int64("segment-mb", 64, "segment size in MiB")
	flag.Parse()
	if *out == "" || *size <= 0 || *connections < 1 || *segmentMB < 1 {
		fmt.Fprintln(os.Stderr, "usage: rangefetch -out PATH -size BYTES [-connections N] [-segment-mb N] < url-and-headers")
		os.Exit(exitUsage)
	}
	target, headers, err := readInput(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(exitUsage)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	j := newJob(target, headers, *out, *size, *segmentMB<<20, *connections)
	code := j.run(ctx)
	stop()
	os.Exit(code)
}

func newJob(target string, headers http.Header, out string, size, segment int64, connections int) *job {
	return &job{
		target:      target,
		headers:     headers,
		out:         out,
		size:        size,
		segment:     segment,
		connections: connections,
		retryBase:   time.Second,
		progress:    os.Stdout,
		client:      newClient(connections),
	}
}

func newClient(connections int) *http.Client {
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 20 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   20 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConns:          connections * 2,
		MaxIdleConnsPerHost:   connections * 2,
		DisableCompression:    true,
		// HTTP/2 would multiplex every range over one TCP connection and cap throughput.
		ForceAttemptHTTP2: false,
		TLSNextProto:      map[string]func(string, *tls.Conn) http.RoundTripper{},
		ReadBufferSize:    1 << 20,
	}
	return &http.Client{Transport: transport}
}

func readInput(r io.Reader) (string, http.Header, error) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	headers := http.Header{}
	target := ""
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if target == "" {
			target = line
			continue
		}
		name, value, ok := strings.Cut(line, ":")
		if !ok || strings.TrimSpace(name) == "" {
			return "", nil, errors.New("invalid header line on stdin")
		}
		headers.Add(strings.TrimSpace(name), strings.TrimSpace(value))
	}
	if err := scanner.Err(); err != nil {
		return "", nil, err
	}
	if target == "" {
		return "", nil, errors.New("missing URL on stdin")
	}
	return target, headers, nil
}

func (j *job) metadataPath() string { return j.out + ".segments.json" }

func (j *job) segmentLength(index int) int64 {
	start := int64(index) * j.segment
	return min(start+j.segment, j.size) - start
}

func (j *job) run(ctx context.Context) int {
	file, err := os.OpenFile(j.out, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "open:", err)
		return exitFailed
	}
	j.file = file
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		fmt.Fprintln(os.Stderr, "stat:", err)
		return exitFailed
	}
	if err := j.loadState(info.Size()); err != nil {
		fmt.Fprintln(os.Stderr, "resume:", err)
		return exitFailed
	}
	if err := file.Truncate(j.size); err != nil {
		fmt.Fprintln(os.Stderr, "truncate:", err)
		return exitFailed
	}

	var pending []int
	var done int64
	for index, ok := range j.completed {
		if ok {
			done += j.segmentLength(index)
		} else {
			pending = append(pending, index)
		}
	}
	j.transferred.Store(done)
	j.mu.Lock()
	err = j.saveLocked()
	j.mu.Unlock()
	if err != nil {
		fmt.Fprintln(os.Stderr, "state:", err)
		return exitFailed
	}

	workCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	var firstErr error
	var failOnce sync.Once
	fail := func(err error) {
		failOnce.Do(func() {
			firstErr = err
			cancel()
		})
	}

	work := make(chan int)
	go func() {
		defer close(work)
		for _, index := range pending {
			select {
			case work <- index:
			case <-workCtx.Done():
				return
			}
		}
	}()

	stopProgress := make(chan struct{})
	progressDone := make(chan struct{})
	go j.reportProgress(stopProgress, progressDone)

	var wg sync.WaitGroup
	for range min(j.connections, max(len(pending), 1)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			buffer := make([]byte, 1<<20)
			for index := range work {
				if err := j.fetchWithRetry(workCtx, index, buffer); err != nil {
					fail(err)
					return
				}
				j.markDone(index)
			}
		}()
	}
	wg.Wait()
	close(stopProgress)
	<-progressDone

	j.mu.Lock()
	saveErr := j.saveLocked()
	j.mu.Unlock()

	switch {
	case ctx.Err() != nil:
		fmt.Fprintln(os.Stderr, "cancelled")
		return exitCancelled
	case firstErr != nil:
		var fatal *fatalError
		if errors.As(firstErr, &fatal) {
			fmt.Fprintln(os.Stderr, fatal.message)
			return fatal.code
		}
		fmt.Fprintln(os.Stderr, describe(firstErr))
		return exitFailed
	case saveErr != nil:
		fmt.Fprintln(os.Stderr, "state:", saveErr)
		return exitFailed
	}
	if err := file.Sync(); err != nil {
		fmt.Fprintln(os.Stderr, "sync:", err)
		return exitFailed
	}
	_ = json.NewEncoder(j.progress).Encode(progressLine{Downloaded: j.size})
	return exitOK
}

func (j *job) resetSegments() {
	j.completed = make([]bool, int((j.size+j.segment-1)/j.segment))
}

// loadState marks segments that an earlier run already wrote.
func (j *job) loadState(existing int64) error {
	data, err := os.ReadFile(j.metadataPath())
	if err == nil {
		var state metadata
		if json.Unmarshal(data, &state) == nil && state.Size == j.size && state.SegmentSize > 0 {
			// Keep the segment layout of the run that wrote these bytes, so a changed
			// segment size setting does not throw away a partial download.
			j.segment = state.SegmentSize
			j.resetSegments()
			for _, index := range state.Completed {
				if index >= 0 && index < len(j.completed) {
					j.completed[index] = true
				}
			}
			return nil
		}
		// State for another object size cannot be trusted segment by segment.
		j.resetSegments()
		return j.file.Truncate(0)
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	j.resetSegments()
	// A .part without segment state was written sequentially from its start.
	prefix := min(existing, j.size)
	for index := range j.completed {
		if min(int64(index+1)*j.segment, j.size) <= prefix {
			j.completed[index] = true
		}
	}
	return nil
}

func (j *job) saveLocked() error {
	state := metadata{Version: 1, Size: j.size, SegmentSize: j.segment, Completed: []int{}}
	for index, ok := range j.completed {
		if ok {
			state.Completed = append(state.Completed, index)
		}
	}
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	temporary := j.metadataPath() + ".tmp"
	if err := os.WriteFile(temporary, data, 0o644); err != nil {
		return err
	}
	j.lastSave = time.Now()
	return os.Rename(temporary, j.metadataPath())
}

func (j *job) markDone(index int) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.completed[index] = true
	if time.Since(j.lastSave) >= 2*time.Second {
		if err := j.saveLocked(); err != nil {
			fmt.Fprintln(os.Stderr, "state:", err)
		}
	}
}

func (j *job) reportProgress(stop <-chan struct{}, done chan<- struct{}) {
	defer close(done)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	encoder := json.NewEncoder(j.progress)
	lastBytes := j.transferred.Load()
	lastTime := time.Now()
	speed := 0.0
	for {
		select {
		case <-stop:
			return
		case now := <-ticker.C:
			current := j.transferred.Load()
			sample := float64(current-lastBytes) / now.Sub(lastTime).Seconds()
			if speed == 0 {
				speed = sample
			} else {
				speed = speed*0.6 + sample*0.4
			}
			lastBytes, lastTime = current, now
			_ = encoder.Encode(progressLine{Downloaded: max(min(current, j.size), 0), Speed: int64(max(speed, 0))})
		}
	}
}

func (j *job) fetchWithRetry(ctx context.Context, index int, buffer []byte) error {
	var last error
	for attempt := range attempts {
		if attempt > 0 {
			delay := j.retryBase << min(attempt-1, 4)
			select {
			case <-time.After(delay):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		written, err := j.fetch(ctx, index, buffer)
		if err == nil {
			return nil
		}
		// Bytes of a failed attempt are written again by the next one.
		j.transferred.Add(-written)
		var fatal *fatalError
		if errors.As(err, &fatal) || ctx.Err() != nil {
			return err
		}
		last = err
	}
	return last
}

func (j *job) fetch(ctx context.Context, index int, buffer []byte) (int64, error) {
	start := int64(index) * j.segment
	end := min(start+j.segment, j.size) - 1

	requestCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	// A connection that stops delivering data is abandoned and the segment retried.
	stall := time.AfterFunc(stallTimeout, cancel)
	defer stall.Stop()

	request, err := http.NewRequestWithContext(requestCtx, http.MethodGet, j.target, nil)
	if err != nil {
		return 0, &fatalError{exitUsage, "invalid URL"}
	}
	for name, values := range j.headers {
		for _, value := range values {
			request.Header.Add(name, value)
		}
	}
	request.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, end))

	response, err := j.client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	switch status := response.StatusCode; {
	case status == http.StatusPartialContent:
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return 0, &fatalError{exitForbidden, fmt.Sprintf("HTTP %d", status)}
	case status == http.StatusNotFound:
		return 0, &fatalError{exitNotFound, "HTTP 404"}
	case status == http.StatusRequestTimeout || status == http.StatusTooEarly ||
		status == http.StatusTooManyRequests || status >= 500:
		return 0, fmt.Errorf("HTTP %d", status)
	default:
		return 0, &fatalError{exitFailed, fmt.Sprintf("HTTP %d for a range request", status)}
	}
	if !strings.HasPrefix(response.Header.Get("Content-Range"), fmt.Sprintf("bytes %d-%d/", start, end)) {
		return 0, &fatalError{exitFailed, "the server ignored the requested byte range"}
	}

	// TLS delivers about 16 KiB per read. Filling the whole buffer before each write keeps
	// the pwrite calls, and the contention of many writers on one file, low.
	body := stallReader{reader: response.Body, timer: stall}
	offset := start
	var written int64
	for offset <= end {
		count, readErr := io.ReadFull(body, buffer[:min(int64(len(buffer)), end+1-offset)])
		if count > 0 {
			if _, err := j.file.WriteAt(buffer[:count], offset); err != nil {
				return written, &fatalError{exitFailed, "write: " + err.Error()}
			}
			offset += int64(count)
			written += int64(count)
			j.transferred.Add(int64(count))
		}
		if readErr == io.EOF || errors.Is(readErr, io.ErrUnexpectedEOF) {
			break
		}
		if readErr != nil {
			return written, readErr
		}
	}
	if offset != end+1 {
		return written, fmt.Errorf("segment %d ended after %d of %d bytes", index, offset-start, end-start+1)
	}
	return written, nil
}

// stallReader restarts the stall timer whenever the connection delivers data.
type stallReader struct {
	reader io.Reader
	timer  *time.Timer
}

func (s stallReader) Read(p []byte) (int, error) {
	count, err := s.reader.Read(p)
	if count > 0 {
		s.timer.Reset(stallTimeout)
	}
	return count, err
}

// describe drops the request URL from transport errors: it may carry a signature.
func describe(err error) string {
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return urlErr.Op + ": " + describe(urlErr.Err)
	}
	return err.Error()
}
