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
- Navigasi **Chat · Papan · Workspace** di header semua halaman: **Chat** = `/` (halaman ini),
  **Papan** = `/agents` (pemantau agent), **Workspace** = `/workspace` (kantor virtual realtime)
- Tombol **Keluar** (kanan header) mengakhiri sesi; tombol di sebelahnya menampilkan user aktif
- Tombol **⚡ Auto** adalah saklar cepat tanpa konfirmasi; **⚙ Setelan** membuka pilihan lengkap (lihat **Auto approve**)

## Upload gambar

Di halaman chat kamu bisa melampirkan gambar ke pesan sebelum dikirim ke Hermes:

- **📎** di composer — pilih satu atau beberapa berkas gambar
- **Ctrl+V** — tempel screenshot atau gambar dari clipboard (selama tidak sedang mode riwayat)
- **Drag & drop** ke area percakapan (`#main`) — lepas berkas gambar di sana

Setelah diunggah, pratinjau kecil (chip) muncul di atas kotak tulis; tekan **×** untuk membatalkan lampiran.
Saat **Kirim**, gambar tidak dikirim sebagai multipart ke gateway — yang dikirim ke `POST /v1/runs` adalah teks user plus penanda
`[Image attached at: /path/absolut/gambar.jpg]` (satu baris per gambar). Hermes mengubah penanda itu menjadi bagian
gambar native untuk model vision. Di gelembung chat yang kamu lihat, hanya teks dan thumbnail; penanda mentah tidak ditampilkan.

Riwayat sesi (`Riwayat` → buka sesi) mem-parsing penanda yang sama dari `content` pesan user, menampilkan thumbnail lewat
`GET /api/image?path=…` kalau path-nya di bawah cache yang diizinkan.

**Batasan:**

| Batas | Nilai |
|---|---|
| Gambar per pesan | maks **4** |
| Ukuran per berkas | maks **12 MB** |
| Format | **JPEG, PNG, GIF, WebP** (dicek magic bytes, bukan ekstensi) |
| Penyimpanan | `/root/.hermes/cache/webchat-images/<YYYYMMDD>/` (mode `600`); berkas >**14 hari** dibersihkan otomatis (best-effort) |
| Gambar dari surface lain | Hanya pratinjau penuh kalau path di bawah `/root/.hermes/cache/webchat-images/` atau `/root/.hermes/cache/images/`; path lain (mis. Telegram) hanya menampilkan nama berkas |

Endpoint lokal (wajib sesi + same-origin, bukan proxy): `POST /api/upload`, `GET /api/image?path=<absolut>`.

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

Pemantau Agent + Workspace (cookie yang sama):

```bash
curl -s -b jar http://127.0.0.1:8650/agents | grep -c 'id="agent-monitor"'   # 1
curl -s -b jar http://127.0.0.1:8650/api/agents/boards                       # daftar board
curl -s -b jar 'http://127.0.0.1:8650/api/agents/board?slug=pjtki'           # counts + tasks
curl -s -b jar 'http://127.0.0.1:8650/api/agents/board?slug=../../etc'       # 400 (ditolak)
curl -s -b jar http://127.0.0.1:8650/workspace | grep -c 'id="workspace-room"'  # 1
curl -s -b jar 'http://127.0.0.1:8650/api/agents/live?slug=pjtki'            # kantor virtual (JSON)
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

## Pemantau Agent (`/agents`)

Halaman kedua di app ini: **memantau pekerjaan agent** di papan kanban Hermes
(status, siapa agent-nya, progress, aktivitas terakhir). Dibuat mobile-first — satu
kolom, target sentuh ≥44 px, tanpa scroll horizontal dari 320 px sampai desktop.
Dibuka lewat tombol **🤖 Agent** di header chat (dan tautan **← Kembali ke chat** untuk
balik ke `/`). Cookie sesi yang sama berlaku; tanpa sesi tetap dialihkan ke `/login`.

Isi halaman:

| Bagian | Isi |
|---|---|
| Header lengket | dua baris yang menempel **bersama-sama** saat halaman digulir: (1) judul **Pemantau Agent**, tombol muat ulang, tautan kembali ke chat; (2) pemilih board + indikator. Keduanya berada dalam satu pembungkus `.head` supaya tidak saling menutupi (dua baris `sticky` terpisah membuat baris judul menutup dropdown board) |
| Indikator | titik status (hijau/merah) + "diperbarui Ns lalu" |
| Chip ringkasan | `Berjalan n` · `Siap n` · `Menunggu n` · `Terhenti n` · `Selesai n` (+ chip lain hanya kalau ada isinya). **Ketuk = filter, ketuk lagi = lepas** |
| Kartu task | badge status berwarna, nama agent, waktu relatif, judul maks 2 baris; untuk task `running` ada titik berdenyut + PID hidup/mati |
| Panel detail | ketuk kartu → sheet: status/agent/PID/heartbeat, path workspace (mono + tombol **Salin**), body lengkap, komentar terakhir, event terakhir, run worker, induk/lanjutan |

Perilaku data:

- **Auto-refresh 10 detik**, tapi **berhenti saat tab tidak terlihat** (`document.hidden`)
  dan menyegarkan **sekali** begitu tab kembali aktif; kalau fetch gagal, daftar yang
  sudah tampil tetap ada dan hanya muncul baris peringatan kecil. Baris peringatan itu
  **hilang sendiri** begitu ada satu refresh yang berhasil (manual atau auto): titik status
  kembali hijau dan label balik ke "diperbarui N dtk lalu" — jadi indikator tidak pernah
  bilang "gagal" sementara datanya sudah segar.
- Board `default` tampil walau belum pernah dipakai; board kosong menampilkan empty-state
  ("Belum ada task di board ini").

### Endpoint JSON (lokal, bukan proxy)

Semua di bawah ini **lokal**: dibaca langsung dari SQLite kanban, tidak diteruskan ke
gateway, jadi API key tidak ikut dan tidak ada rute proxy baru. Tetap wajib **sesi login**
dan **same-origin** (Origin/Referer dicek → 403; tanpa sesi → 401/302).

| Endpoint | Isi |
|---|---|
| `GET /api/agents/boards` | array `[{slug, name, description, task_count, running_count, current}]` — `default` lebih dulu, lalu board lain terurut; board yang diarsipkan dilewati |
| `GET /api/agents/board?slug=<slug>&limit=<n>` | `{board, generated_at, counts, tasks, has_db, returned, truncated}`; `limit` 1–200 (default 150) |
| `GET /api/agents/task?slug=<slug>&id=<task_id>` | satu task lengkap: `body` penuh, `comments` (10 terakhir), `events` (20 terakhir), `runs` (5 terakhir) |

Bentuk `tasks[]`: `id, title, status, assignee, priority, created_at, started_at,
completed_at, worker_pid, worker_alive, last_heartbeat_at, workspace_path, workspace_kind,
branch_name, block_kind, body_preview (≤400 char), body_truncated, comment_count,
last_comment, last_event, parents, children` — **body penuh tidak pernah dikirim di
endpoint daftar** (bisa sangat panjang); ambil lewat `/api/agents/task`.

`counts` memuat `running/ready/todo/blocked/review/done/archived` + `total`; `running` di
atas pada daftar task dan **task arsip tidak ikut dikirim** (hanya dihitung). Ini sebabnya
angka `counts` cocok dengan `hermes kanban --board <slug> list`, sementara `total` juga
menghitung arsip seperti `hermes kanban boards list`.

Detail teknis & keamanan:

- **Read-only**: setiap koneksi dibuka `sqlite3.connect("file:…?mode=ro", uri=True)` dengan
  timeout kecil + **retry sekali** kalau DB sedang terkunci (WAL). Tidak ada satu pun jalur
  tulis ke `kanban.db`.
- `slug` divalidasi `^[a-z0-9][a-z0-9_-]{0,63}$` (huruf besar dinormalkan ke kecil) dan
  `id` task divalidasi regex; slug yang tidak sah → **400**, board tidak dikenal → **404**,
  jadi `slug=../../etc` ditolak 400 dan bukan 500. Path DB juga dicek `relative_to()` ke
  direktori `boards/` sebagai sabuk pengaman kedua.
- Lokasi papan mengikuti Hermes: `default` = `<root>/kanban.db`, board lain =
  `<root>/kanban/boards/<slug>/kanban.db`, board aktif dari `<root>/kanban/current`
  (`<root>` = `~/.hermes`). Pin `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` sengaja **tidak**
  dipakai — itu penanda worker (satu papan), sedangkan halaman ini melihat semua papan.
- `worker_alive` = status `running` **dan** `/proc/<pid>` ada. Tidak ada dependensi baru
  (stdlib Python + JS bawaan browser, tanpa CDN).

## Workspace (`/workspace`) — kantor virtual realtime

Halaman ketiga: **kantor** berisi satu meja per profil agent. Kelihatan sekilas siapa
yang **sedang bekerja**, siapa yang **menganggur**, siapa yang **terhenti** (butuh
keputusan), dan siapa yang **baru saja selesai**. Mobile-first, sprite SVG + animasi CSS
(satu file, tanpa dependency/CDN/gambar eksternal).

Dibuka lewat navigasi **Workspace** di header (dari `/` atau `/agents`). Cookie sesi yang
sama; tanpa sesi dialihkan ke `/login`.

### Arti warna & state

| State | Warna | Di ruangan | Sumbernya (bukan laporan agent, tapi fakta) |
|---|---|---|---|
| **working** | hijau | duduk di meja, animasi mengetik dua frame, layar monitor menyala, gelembung = **judul task** (1 baris, dipotong `…`) | ada task `running` dengan `assignee = profil` **dan** `/proc/<worker_pid>` ada |
| **blocked** | merah | tetap di mejanya, gelembung **"butuh keputusan"** | ada task `blocked` untuk profil itu (menang atas `idle`) |
| **done** | abu | di mejanya, gelembung **"selesai"** | task terakhirnya `completed_at` < **120 detik** lalu; setelah itu kembali `idle` |
| **idle** | biru | berdiri/mengembara di **sudut kopi** (nama profil tertulis di bawah karakternya), gelembung **"nganggur"** | profil terdaftar, tapi tidak memenuhi salah satu di atas |
| **offline** | abu gelap | kursi kosong (tanpa karakter), label status di bawah meja | assignee muncul di papan tapi tidak punya direktori profil |

Aturan yang penting: **PID mati bukan "bekerja"**. Kalau papan masih menulis `running`
tetapi proses `worker_pid` sudah tidak ada di `/proc`, agen itu ditampilkan **idle** —
ruangan ini tidak berbohong demi menyenangkan angka papan.

Isi lain di ruangan: papan tulis (nama board + `N agen kerja · M nganggur` + `X terhenti ·
Y selesai · Z jalan`), jendela malam, **jam dinding yang menunjukkan waktu server** (UTC
di mesin ini), meja kopi + tanaman, dan pintu di kanan bawah. Nilai `Z jalan` = jumlah
**task** berstatus `running` di board itu (hover teksnya untuk penjelasan), sedangkan
angka lain = jumlah **agen/meja** — bedanya muncul saat satu profil memegang dua task
`running` sekaligus.

### Interaksi

- **Ketuk/klik meja atau karakter** → panel bawah (bottom sheet): profil, role (dari
  `profile.yaml`), state + "sejak kapan", task + **ID**, jumlah task jalan, **PID** +
  hidup/tidak, event terakhir, **3 aktivitas terakhir** (komentar/event, ≤80 karakter),
  dan tombol **Lihat di Papan →** yang membuka `/agents?slug=<board>`. Tutup lewat ✕,
  ketuk latar, atau `Esc`.
- **Pemilih board** di atas ruangan (sama seperti halaman Papan); pilihannya diingat di
  localStorage (`hermes-workspace-v1`).
- **Roster** di bawah ruangan (bisa dilipat): daftar agen, state, dan judul task-nya.
  Ini juga yang membuat halaman tetap berguna kalau animasi dimatikan.
- **Live**: polling `/api/agents/live` tiap **3 detik**; berhenti saat tab tidak aktif
  (`document.hidden`) dan menyegarkan sekali begitu tab kembali. Indikator "live ·
  diperbarui N dtk lalu" berdenyut hijau; kalau fetch gagal statusnya "terputus" dan
  ruangan **tidak dikosongkan** (data terakhir tetap tampil).
- **Lebar meja**: 2 kolom di layar sempit, 3 kolom mulai 560 px; ruangan menskala penuh
  (SVG `viewBox`), jadi **tidak ada scroll horizontal** dari 320 px sampai desktop.
- **`prefers-reduced-motion: reduce`** → semua animasi karakter/denyut dimatikan; state
  tetap jelas lewat warna + label teks di bawah meja + gelembung status.

### Cara menambah agen

Tidak ada konfigurasi khusus: **satu meja = satu profil Hermes**.

1. Buat profil seperti biasa, mis. `hermes profile create pjtki-qa`. Meja baru muncul di
   poll berikutnya (≤3 detik) tanpa reload.
2. Direktori `~/.hermes/profiles/<nama>/` dianggap profil kalau berisi `config.yaml`
   atau `profile.yaml`; direktori non-profil (cache, backups, home) dilewati. Profil
   `default` selalu punya meja.
3. Teks **role** di panel detail diambil dari `description:` pada
   `~/.hermes/profiles/<nama>/profile.yaml` (kalau ada). Nama profil selalu jadi label meja.
4. Agent muncul di meja itu begitu ada task di board terpilih yang `assignee`-nya sama
   dengan nama profil — tidak perlu hal lain: keadaannya dibaca dari kanban.db + `/proc`.

### Endpoint `GET /api/agents/live?slug=<slug>`

Lokal (bukan proxy), read-only, wajib sesi + same-origin — sama seperti `/api/agents/board`.

```json
{
  "board": {"slug": "pjtki", "name": "PJTKI Dev", "current": true},
  "generated_at": 1792206305,
  "agents": [{"profile": "pjtki-qa", "role": "Worker QA PJTKI …", "state": "working",
              "task_id": "t_06a74dc0", "task_title": "QA-Q5 Report …", "task_status": "running",
              "since": 1792205658, "worker_pid": 473224, "worker_alive": true,
              "last_event": {"kind": "heartbeat", "at": 1792206298},
              "recent": ["pjtki-qa: menjalankan pm2 restart …"], "tasks_running": 2}],
  "counts": {"working": 2, "blocked": 0, "done": 0, "idle": 4, "offline": 0,
             "working_agents": 1, "running_tasks": 2, "agents": 5},
  "has_db": true
}
```

- `counts.working` = jumlah **task** `running` yang `worker_pid`-nya benar-benar hidup →
  angka ini bisa dibandingkan langsung dengan `hermes kanban --board <slug> list`.
  `counts.working_agents` = jumlah **meja** berstatus bekerja (beda hanya saat satu profil
  memegang dua task `running` sekaligus); `idle`/`blocked`/`done`/`offline` dihitung per agen.
- `recent` = maksimal 3 komentar/event terakhir task itu (≤80 karakter; `heartbeat` hanya
  dipakai mengisi sisa slot supaya daftar tidak penuh denyut liveness).
- **Isi `body` task tidak pernah dikirim** ke halaman ini — hanya judul (≤140 karakter),
  ID, status, PID, dan waktu.
- Slug divalidasi regex yang sama seperti endpoint lain (tidak sah → **400**, board tidak
  dikenal → **404**, tanpa sesi → **401**/302, cross-origin → **403**).

Catatan operasional: `/workspace` dan `/api/agents/live` hidup di `server.py`, jadi setelah
perubahan `server.py` service perlu di-restart (`systemctl restart hermes-webchat`) —
koneksi browser yang sedang terbuka terputus sesaat (gateway tetap hidup, riwayat aman).
Perubahan `static/*.html` **tidak** butuh restart. Restart dijalankan **dari shell biasa
(SSH)**: guard terminal agent menolak perintah stop/restart service saat tidak ada user
yang bisa menyetujui permintaan itu.

## File

| File | Isi |
|---|---|
| `server.py` | Server + reverse proxy + login (stdlib Python saja, tanpa dependensi) + endpoint `/api/agents/*` |
| `static/index.html` | Seluruh UI chat (HTML + CSS + JS, tanpa CDN) |
| `static/agents.html` | Halaman **Pemantau Agent** (`/agents`) — papan kanban, mobile-first |
| `static/workspace.html` | Halaman **Workspace** (`/workspace`) — kantor virtual realtime (sprite SVG + animasi CSS) |
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

Halaman **Pemantau Agent** (`/agents`) dan **Workspace** (`/workspace`) tidak lewat gateway
sama sekali: `server.py` membaca file SQLite kanban Hermes langsung (read-only) dan
memeriksa `/proc/<pid>` untuk liveness. Jadi keduanya tetap bisa dibuka walau API server
sedang mati — yang tidak jalan hanya halaman chat-nya.

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
