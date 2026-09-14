# Instant Models — dokumentacja techniczna i dalsze prace

## 1. Cel i aktualny stan

Instant Models jest płatnym, opcjonalnym źródłem modeli dla launchera AIMODELKI ALL IN ONE na RunPodzie. Nie jest osobnym instalatorem. Użytkownik aktywuje token, a następnie nadal korzysta z czterech kart katalogu: Image Generation, Dataset Generator, Image Edit i Video Motion Control.

- bez tokenu: `Standard Download` z publicznych źródeł;
- z aktywnym tokenem: `Instant Download` z prywatnego Cloudflare R2;
- tylko jeden pakiet może być instalowany jednocześnie;
- Image Edit pobiera gated model FLUX z R2, natomiast bez Instant Models wymaga jego ręcznego dodania.

Aktualny obraz: `aimodelki/aimodelki-allin1:1.3.5`.

## 2. Przepływ techniczny

1. Użytkownik generuje token `im_live_…` w panelu konta AIMODELKI.
2. Launcher wysyła token do centralnego API i pobiera manifest:
   `GET /api/v1/instant-models/manifest` z nagłówkiem `Authorization: Bearer …`.
3. Token jest zapisany w `/workspace/.instant-models/credentials` (uprawnienia `0600`), dzięki czemu pozostaje na RunPod Volume Disk.
4. Kliknięcie karty katalogu wybiera pakiet. Launcher ponownie autoryzuje token i wymaga kompletnego manifestu R2 dla całego pakietu — nie miesza R2 ze źródłami publicznymi.
5. Dla każdego pliku launcher pobiera krótkotrwały URL:
   `POST /api/v1/instant-models/files/{fileId}/download-url`.
6. Backend kontenera pobiera plik bez udziału przeglądarki. Obsługuje HTTP Range, do 64 segmentów równolegle, retry, `.part`, wznowienie, SHA-256 i atomową zmianę nazwy.
7. Poprawny istniejący plik jest pomijany. Po anulowaniu przycisk **Wznów pobieranie** kontynuuje zapisane segmenty.

R2 jest prywatne. Credentiale R2 istnieją wyłącznie w centralnym serwisie. Launcher otrzymuje tylko presigned URL o krótkim TTL.

## 3. Centralny serwis AIMODELKI

Istniejące elementy:

- modele Prisma: `InstantModelsSubscription` i `InstantModelsToken`;
- token przechowywany w bazie wyłącznie jako SHA-256 z `INSTANT_MODELS_TOKEN_PEPPER`;
- aktywna subskrypcja i jej data końcowa są sprawdzane przy każdym żądaniu;
- `401` — nieprawidłowy token, `402` — nieaktywna subskrypcja, `429` — limit prób, `503` — awaria usługi;
- panel konta: status subskrypcji, jednorazowe pokazanie jawnego tokenu, regeneracja i unieważnienie;
- manifest ogranicza pakiety według pola `plan` (`all` albo konkretny pakiet);
- testowe/ręczne nadanie dostępu: `npm run instant-models:grant -- user@example.com [plan] [dni]`.

Najważniejsze pliki serwisu znajdują się w `_instant_models_service_stage` oraz kopii produkcyjnej `_instant_models_prod_deploy`:

- `prisma/schema.prisma`
- `src/lib/instant-models/auth.ts`
- `src/lib/instant-models/keys.ts`
- `src/lib/instant-models/manifest.ts`
- `src/lib/instant-models/r2.ts`
- `src/app/api/v1/instant-models/**`
- `src/components/account/instant-models-card.tsx`

## 4. Stripe — funkcje do wdrożenia

1. Utworzyć w Stripe osobny produkt **Instant Models** i miesięczny recurring Price.
2. Dodać konfigurację, minimum:
   - `INSTANT_MODELS_STRIPE_PRICE_ID`
   - `INSTANT_MODELS_STRIPE_WEBHOOK_SECRET`
   - opcjonalnie osobny `INSTANT_MODELS_STRIPE_SECRET_KEY`; w przeciwnym razie użyć istniejącego konta Stripe.
3. Dodać uwierzytelniony endpoint `POST /api/instant-models/checkout`, tworzący Checkout Session z `mode=subscription`, identyfikatorem użytkownika oraz metadanymi `product=instant_models` i `plan`.
4. Dodać Customer Portal do zmiany metody płatności, anulowania i ponownego uruchomienia subskrypcji.
5. Dodać webhook, najlepiej `POST /api/instant-models/stripe/webhook`. Musi:
   - weryfikować podpis na surowym body;
   - mieć osobny sekret webhooka;
   - używać klucza idempotencji `instant-models:{event.id}` w `processed_events`;
   - obsługiwać `checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.paid` i `invoice.payment_failed`;
   - aktualizować `stripeCustomerId`, `stripeSubscriptionId`, okres, `cancelAtPeriodEnd` i status.
6. Mapowanie statusów:
   - `active`/`trialing` → `ACTIVE`;
   - `past_due`, `unpaid`, `incomplete`, `paused` → `BLOCKED`;
   - `canceled`, `incomplete_expired` → `CANCELED`;
   - lokalnego `REVOKED` nie wolno automatycznie odblokować webhookiem.
7. Token powinien pozostać stały między opłaconymi okresami. Wygaśnięcie płatności blokuje autoryzację, ale nie wymusza generowania nowego tokenu po odnowieniu.
8. Dodać okresowy reconciliation job pobierający stan subskrypcji ze Stripe na wypadek utraconego webhooka.

Nie wolno nadawać dostępu na stronie sukcesu po checkout. Jedynym źródłem prawdy jest podpisany webhook Stripe. Obecny projekt ma już dwa inne webhooki Stripe (kurs i PromptShot), dlatego Instant Models musi mieć własny sekret i przestrzeń idempotencji albo zostać świadomie dodany do jednego centralnego routera zdarzeń.

## 5. Kryteria akceptacji płatności

- zakup aktywuje subskrypcję i umożliwia wygenerowanie tokenu;
- anulowanie na koniec okresu zachowuje dostęp do `currentPeriodEnd`;
- brak płatności blokuje manifest i URL-e (`402`);
- ponowienie płatności przywraca ten sam token;
- duplikat webhooka nie tworzy drugiej subskrypcji;
- użytkownik nie uzyska dostępu przez zmianę metadanych po stronie klienta;
- testy obejmują checkout, wszystkie przejścia statusów, duplikaty, spóźnione zdarzenia i błędny podpis.
