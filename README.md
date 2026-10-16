# Hermes Web Chat

Chat UI satu halaman untuk Hermes Agent. Bukan TUI di dalam browser — ini UI chat
sungguhan yang bicara ke **API server Hermes** yang OpenAI-compatible.

## Cara pakai

- **http://192.168.1.4:8650** (dari LAN) atau http://127.0.0.1:8650 (di server)
- Login dulu (lihat bagian **Login**) — tanpa sesi, semua halaman dialihkan ke `/login`
- Enter = kirim, Shift+Enter = baris baru
- Tool call muncul sebagai chip inline; reasoning muncul sebagai blok yang bisa dilipat
- Tombol **Chat baru** mengosongkan percakapan; riwayat disimpan di localStorage browser
- Tombol **Riwayat** membuka daftar sesi (semua sumber); **Lanjutkan chat** di banner
  menjadikan sesi terpilih sebagai percakapan aktif — pesan baru masuk ke sesi yang sama
- Tombol **Keluar** (kanan header) mengakhiri sesi; tombol di sebelahnya menampilkan user aktif
- Tombol **⚡ Auto** adalah saklar cepat tanpa konfirmasi; **⚙ Setelan** membuka pilihan lengkap (lihat **Auto approve**)

## Login

Halaman ini menempelkan `API_SERVER_KEY` gateway ke tiap request, jadi **tanpa login
siapa pun yang bisa membuka port 8650 memakai agent sebagai root**. Karena itu seluruh
route kecuali `/login` dan `/healthz` mensyaratkan sesi.

### Kredensial

Disimpan di `auth.json` (mode `600`, satu user): hash **PBKDF2-SHA256** 210.000 iterasi
+ salt acak, plus `secret` untuk menandatangani cookie. Tidak ada password plaintext di
disk dan tidak ada kredensial di browser (cookie-nya `HttpOnly`, jadi JS pun tidak bisa
membacanya).

```bash
cd /root/.hermes/webchat
python3 server.py --set-password              # ganti password (interaktif, min 8 karakter)
python3 server.py --set-password --random     # password acak, langsung dicetak
python3 server.py --set-password --user nama  # ganti username juga
python3 server.py --show-auth                 # cek user/path tanpa membuka hash
python3 server.py --logout-all                # ganti signing secret => semua sesi hangus
```

Perubahan `auth.json` berlaku **tanpa restart** (dibaca ulang berdasarkan mtime).
Ganti username lewat `--set-password --user`; karena login membandingkan username juga,
user lama otomatis tidak bisa masuk lagi.

### Sesi

- Cookie `hermes_chat`, `HttpOnly` + `SameSite=Lax`, umur **7 hari**
  (`WEBCHAT_SESSION_TTL`). Tokennya stateless (HMAC-SHA256), jadi sesi selamat dari
  restart service.
- **Keluar** mencabut token di sisi server (nonce dicatat sampai token kedaluwarsa),
  bukan sekadar menghapus cookie di browser — token yang sudah dicuri pun mati. Sesi di
  perangkat lain tidak ikut mati; untuk mematikan semuanya pakai `--logout-all`.
- Salinan token bisa dipakai klien non-browser: `Authorization: Bearer <token>`.

### Batas percobaan

5 kali gagal dalam 15 menit per IP → **HTTP 429** selama jendela itu, dan password yang
benar pun ditolak sampai jendela lewat. Ubah lewat `WEBCHAT_MAX_FAILS` /
`WEBCHAT_LOCK_SECONDS`. Tiap kegagalan juga ditahan ~0,4 detik.

### Anti-CSRF

Selain `SameSite=Lax`, setiap POST memeriksa header `Origin`/`Referer`: kalau ada dan
host-nya bukan host request ini, jawabannya **403**. Parameter `?next=` hanya menerima
path lokal (`//host` ditolak), jadi tidak bisa dipakai open redirect.

### Cek dari terminal

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8650/          # 302 -> /login
curl -s -c jar -X POST http://127.0.0.1:8650/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"…"}'                               # 200 + Set-Cookie
curl -s -b jar -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8650/   # 200
```

### Menampilkan laporan: tabel, kode, dan pohon

Balasan agent dirender sebagai markdown (implementasi sendiri, tanpa library/CDN):

| Yang ditulis agent | Yang tampil |
|---|---|
| Tabel GFM (`\| a \| b \|` + baris `\|---\|---:\|`) | Tabel sungguhan: header, baris zebra, kolom angka rata kanan + font mono, bisa di-scroll mendatar |
| Blok kode berspasi-label bahasa | Kotak kode dengan bar atas (nama bahasa + tombol **Salin**) dan pewarnaan ringan: string, komentar, angka, kata kunci/perintah umum |
| Pohon: keluaran `tree`, `ls -R`, atau menu bertingkat (blok kode tanpa bahasa) | Tampilan pohon dengan ikon 📁/📄 dan garis cabang; folder bisa dibuka/tutup dengan klik |
| Daftar bertingkat (bullet/angka yang di-indentasi) | `<ul>`/`<ol>` bersarang |
| `**tebal**`, `*miring*`, `~~coret~~`, `` `kode` ``, tautan, `> kutipan`, `---` | Sesuai markdown |

Teks biasa tidak pernah salah dibaca sebagai pohon: perlu minimal dua baris bergaris cabang
(`├──`/`└──`/`` `-- ``).

Tombol **Salin** menyalin seluruh isi blok kode atau pohon. Di origin http biasa
(`http://192.168.1.4:8650`) `navigator.clipboard` tidak tersedia, jadi halaman memakai
fallback `document.execCommand('copy')` — itu butuh klik sungguhan dari pengguna.

### Pemilih provider & model

Klik tombol model di header. Daftar diambil dari `GET /api/model/options` pada API server
(inventaris yang sama dengan dashboard/TUI), dicari dengan kotak pencarian, dan provider
yang terautentikasi diurutkan lebih dulu. Pilihan disimpan di localStorage dan dikirim
per-request sebagai `model` **dan** `provider` — provider eksplisit selalu dihormati,
jadi tidak perlu menyalakan `gateway.platforms.api_server.direct_model_requests`.

Catatan: provider hanya "terautentikasi", belum tentu **punya kredit**. Memilih model
dari provider dengan saldo nol akan menjawab dengan pesan penagihan dari provider itu
(tampil sebagai jawaban di transkrip, bukan sebagai error UI). Untuk mesin ini,
`opencode-go` yang punya kredit.

### Proses & konfirmasi (jalur Runs API)

Turn tidak memakai `/v1/chat/completions` lagi, tapi **Runs API**:
`POST /v1/runs` membuat run, lalu `GET /v1/runs/{id}/events` mengalirkan eventnya. Di
sinilah informasi proses yang sesungguhnya dikirim, jadi UI bisa menampilkan:

| Event | Yang ditampilkan |
|---|---|
| `tool.started` | chip tool + ringkasan argumen (`preview`, mis. `ls -la /path`) |
| `tool.completed` | durasi detik + penanda **gagal** kalau tool error |
| `approval.request` | **kartu konfirmasi**: perintah lengkap, alasan deteksi, dan tombol pilihan |
| `approval.responded` | kartu berubah jadi "Dijawab: …" |
| `subagent.start` / `subagent.complete` | chip subagent beserta statusnya |
| `message.delta` | teks jawaban (field-nya `delta`, bukan `text`) |
| `run.completed` | jawaban final + usage |

Menekan **Stop** memanggil `POST /v1/runs/{id}/stop` (bukan sekadar memutus koneksi),
jadi agent benar-benar dihentikan di sisi server.

**Kartu konfirmasi.** Kalau Hermes memblokir sebuah aksi dan menunggu keputusan, UI
menampilkan perintahnya dan tombol sesuai yang ditawarkan server
(`choices`): *Terima sekali* / *Terima untuk sesi ini* / *Selalu terima* / *Tolak*.
Jawabannya dikirim ke `POST /v1/runs/{id}/approval` bersama `request_id` dan
`execution_generation` — identitas tepat yang diminta server, supaya jawaban basi atau
tertukar ditolak dengan 409, bukan dieksekusi keliru. Kalau tidak dijawab, server gagal
tertutup (tolak) setelah `approvals.timeout` (default 5 menit).

### Kenapa konfirmasi kadang tidak muncul

`approvals.mode` menentukan apakah sebuah perintah yang terdeteksi berbahaya langsung
ditanyakan ke kamu:

| Mode | Perilaku |
|---|---|
| `smart` (default) | Dinilai dulu oleh smart approver. Perintah yang dinilai aman (mis. `rm -rf` di `/tmp`) **dijalankan tanpa kartu**; yang benar-benar dinilai berbahaya baru memunculkan kartu |
| `manual` | **Setiap** perintah yang cocok pola bahaya memunculkan kartu — paling mirip TUI |

Saat halaman ini dibangun, `approvals.mode` diset ke `manual` untuk pengujian dan
**belum dikembalikan** (perintah pembaliknya butuh persetujuanmu). Untuk kembali ke
default:

```bash
hermes config unset approvals.mode
```

Catatan penting: pada platform `api_server`, konfirmasi hanya muncul karena stream event
mendaftarkan *approval notifier*. Itu sebabnya halaman ini wajib memakai
`/v1/runs/{id}/events` — pada jalur tanpa notifier, Hermes tidak menunggu siapa pun dan
langsung menolak (`approvals.unattended_mode`).

### Auto approve (tanpa konfirmasi)

Ada di **⚙ Setelan** (kanan header), tersimpan per-browser:

**Kalau Hermes minta konfirmasi**
| Pilihan | Arti |
|---|---|
| **Jalan tanpa konfirmasi (auto terima)** — default | Halaman menjawab sendiri setiap permintaan konfirmasi; tidak ada yang menunggu klikmu |
| Tanya dulu (tampilkan kartu) | Run berhenti sampai kamu menekan tombol di kartu |

**Scope jawaban otomatis** (muncul saat mode auto aktif)

| Scope | Arti |
|---|---|
| **Terima untuk sesi ini** — default | Berlaku sampai sesi chat ini selesai; tidak ada yang ditulis ke disk Hermes |
| Terima sekali saja | Hanya perintah itu; permintaan berikutnya dijawab otomatis lagi |
| Selalu terima (permanen) | Menulis allowlist permanen ke disk Hermes — perintah serupa tidak ditanya lagi kapan pun |

Tombol **⚡ Auto** di header adalah saklar cepat untuk mode auto/tanya-dulu (teksnya
menunjukkan keadaan sekarang); ⚙ membuka pilihan lengkap termasuk scope. Saat mode auto,
kartu konfirmasi tetap dicetak di transkrip sebagai catatan
("⚡ Konfirmasi dijawab otomatis" + scope yang dipakai), jadi kamu masih bisa melihat
perintah apa yang diloloskan.

Setelan ini hanya memengaruhi **halaman web ini** (localStorage browser yang kamu pakai);
TUI, dashboard, dan Open WebUI punya kebijakannya sendiri.

Catatan: di mesin ini `~/.hermes/config.yaml` sudah `approvals.mode: "off"` +
`unattended_mode: approve`, jadi gateway memang tidak pernah memunculkan permintaan
konfirmasi (pure bypass). Setelan di halaman ini adalah lapisan kedua: kalau mode
dikembalikan ke `smart`/`manual`, halaman ini tetap tidak berhenti menunggu. Perintah yang
masuk "hardline block" tetap ditolak — bukan karena mode, tapi karena kebijakan yang memang
tidak bisa dilewati.

### Riwayat sesi

Tombol **Riwayat** menampilkan sesi dari `GET /api/sessions` — **semua** sumber, jadi
percakapan dari TUI/dashboard, CLI, dan `api_server` semuanya terlihat, bukan hanya yang
dibuat halaman ini. Klik satu sesi untuk membaca transkripnya (`GET
/api/sessions/{id}/messages`), lengkap dengan chip tool dan hasil tool-nya.

Tombol **Lanjutkan chat** di banner mengubah mode riwayat menjadi chat hidup: transkrip
yang sedang dilihat menjadi konteks percakapan, `session_id` diarahkan ke sesi itu, dan
pesan berikutnya dikirim lewat `POST /v1/runs` dengan `session_id` tersebut — jadi pesan
baru ditambahkan **ke sesi yang sama** (bukan fork/salinan). Sesi dari permukaan lain
(CLI/TUI/dashboard) ikut bisa dilanjutkan: runs API menerima `session_id` mana pun yang
ada di DB dan menulis lanjutannya di sana (diuji pada sesi `cli`: 22 → 24 pesan, `source`
tetap `cli`). Tombol **Tutup** tetap ada untuk sekadar membaca tanpa mengubah sesi.

## File

| File | Isi |
|---|---|
| `server.py` | Server + reverse proxy + login (stdlib Python saja, tanpa dependensi) |
| `static/index.html` | Seluruh UI chat (HTML + CSS + JS, tanpa CDN) |
| `static/login.html` | Halaman login |
| `auth.json` | Username + hash PBKDF2 + signing secret (mode `600`, jangan di-commit) |
| `~/.config/systemd/user/hermes-webchat.service` | Unit systemd user (enabled, linger aktif) |

## Arsitektur

```
browser ──► :8650 server.py ──► 127.0.0.1:8642 (API server Hermes) ──► AIAgent
               │      │
               │      └── menempelkan Authorization: Bearer ***
               └── gerbang login: cookie sesi HMAC (HttpOnly) atau Bearer <token>
```

Kunci API **tidak pernah** sampai ke browser: browser bicara ke server ini pada origin
yang sama, dan server ini yang menempelkan bearer-nya. Karena itu tidak perlu CORS, dan
tidak ada kredensial di localStorage — berbeda dengan menembak 8642 langsung dari browser.

Percakapan memakai header `X-Hermes-Session-Id`, jadi sesi, transcript, dan memory
Hermes berlanjut antar pesan, bukan mulai dari nol setiap kali.

## Prasyarat yang wajib

**Adapter `api_server` harus hidup di port 8642.** Kalau belum, halaman ini
menampilkan status `offline` dan pesan yang menjelaskan sebabnya.

Cek:

```bash
curl -s http://127.0.0.1:8642/health      # {"status":"ok"}
```

Kalau tidak ada respons, gateway belum boot dengan adapter itu — API server adalah
platform gateway dan hanya dibaca saat boot; tidak ada hot reload. Nyalakan dengan salah
satu cara berikut, **lalu halaman ini pulih sendiri dalam ≤20 detik** (polling status),
tanpa perlu reload:

1. Dashboard Hermes → **System** → tombol **Restart Gateway**
2. `hermes gateway restart` dari shell di luar gateway (SSH/console)
3. `/restart` sebagai pesan gateway

Konfigurasi yang sudah terpasang di `~/.hermes/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<32 karakter>
```

`gateway/config_env.py::_api_server()` menyalakan platform ini begitu `API_SERVER_KEY`
ada dan panjangnya ≥16 karakter — flag `ENABLED` sendiri tidak wajib.

## Kenapa bukan agent tool yang me-restart

`tools/terminal_tool_guards.py::gateway_lifecycle_block()` menolak perintah terminal
(yang datang dari dalam proses gateway) untuk restart/stop gateway: SIGTERM akan
membunuh proses pemanggilnya sebelum perintah selesai. Guard-nya menyarankan shell
terpisah — dan itulah kenapa langkah di atas adalah aksi manusia. Jangan dilewati.

## Uji mandiri (tanpa mengganggu instance produksi)

`server.py` bisa diarahkan ke upstream OpenAI-compatible mana pun:

```bash
HERMES_API_URL=http://127.0.0.1:8645 API_SERVER_KEY=hermes-proxy \
WEBCHAT_HOST=127.0.0.1 WEBCHAT_PORT=8651 /usr/bin/python3 server.py
```

Dipakai untuk membuktikan jalur streaming-nya (potongan SSE harus datang bertahap, bukan
sekali jadi).
