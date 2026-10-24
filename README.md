# Hermes Web Chat

Chat UI satu halaman untuk Hermes Agent. Bukan TUI di dalam browser — ini UI chat
sungguhan yang bicara ke **API server Hermes** yang OpenAI-compatible.

## Cara pakai

- **http://192.168.1.4:8650** (dari LAN) atau http://127.0.0.1:8650 (di server)
- Login dulu (lihat bagian **Login**) — tanpa sesi, semua halaman dialihkan ke `/login`
- Enter = kirim, Shift+Enter = baris baru
- **Sidebar kiri** berisi daftar percakapan (localStorage), tombol **Chat baru**, pencarian, dan
  hapus per item; di layar sempit jadi drawer dengan overlay. Tombol ☰ melipat/membukanya
- Tool call muncul sebagai chip inline; reasoning muncul sebagai blok yang bisa dilipat; tiap
  pesan punya tombol **Salin**; gambar di transkrip bisa diklik (lightbox)
- **Lampiran**: 📎 / Ctrl+V / drag & drop — gambar dan dokumen (docx, pdf, xlsx, pptx, txt, md,
  csv, json, log), maks 25 MB per berkas. Lihat **Lampiran: gambar & dokumen**
- Tombol **Riwayat** membuka daftar sesi (semua sumber); **Lanjutkan chat** di banner
  menjadikan sesi terpilih sebagai percakapan aktif — pesan baru masuk ke sesi yang sama
- Navigasi **Chat · Papan · Workspace** di header semua halaman: **Chat** = `/` (halaman ini),
  **Papan** = `/agents` (pemantau agent), **Workspace** = `/workspace` (kantor virtual realtime)
- Tombol **Keluar** (kiri bawah sidebar) mengakhiri sesi; nama user aktif tampil di sebelahnya
- Tombol **⚡ Auto** adalah saklar cepat tanpa konfirmasi; **⚙ Setelan** membuka pilihan lengkap (lihat **Auto approve**)

## Lampiran: gambar & dokumen

Tombol **📎** di composer, **Ctrl+V** (tempel gambar dari clipboard), atau **drag & drop** ke
jendela. Setelah terunggah, chip pratinjau muncul di atas kotak tulis; tekan **×** untuk
membatalkan. Lampiran dikirim ke **server ini**, bukan langsung ke API server Hermes — endpoint
`/v1/runs` hanya menerima part teks dan `image_url` (*"uploaded files and document inputs are not
supported on this endpoint"*). Jadi server menyimpan file, mengekstrak teksnya, lalu UI menyusun
prompt teks.

| Jenis | Ekstensi | Perlakuan |
|---|---|---|
| Gambar | `png jpg jpeg gif webp bmp` | divalidasi dari **magic bytes** (bukan ekstensi), thumbnail + lightbox di UI, path absolut + penanda `[Image attached at: /path]` dikirim ke agent; UI juga menambahkan instruksi memakai tool `vision_analyze` sebagai jalur cadangan |
| Office | `docx xlsx pptx` | teks diekstrak dengan `zipfile` + `xml.etree` (stdlib) |
| PDF | `pdf` | `pdftotext -layout` kalau tersedia; kalau tidak, fallback zlib + regex stream |
| Teks | `txt md csv json log` | dibaca UTF-8 dengan `errors=replace` |

**Batasan:**

| Batas | Nilai |
|---|---|
| Ukuran per berkas | maks **25 MB** (`WEBCHAT_MAX_UPLOAD_MB`) |
| Gambar per pesan | maks **4** (ditegakkan di UI) |
| Panjang teks diekstrak | 60.000 karakter (`WEBCHAT_MAX_TEXT_CHARS`), respons menandai `truncated` |
| Format gambar | JPEG, PNG, GIF, WebP, BMP — dicek dari isi berkas |
| Penyimpanan | `uploads/<YYYY-MM-DD>/<uuid8>_<nama-aman>` (folder `0700`, berkas `0600`) |

Penanda `[Image attached at: …]` tetap ditulis ke teks pesan supaya transkrip lama maupun baru
menampilkan thumbnail saat dibuka lewat **Riwayat**; placeholder `[screenshot]` dibuang dari
tampilan.

### Cara kerja ekstraksi docx

1. `word/document.xml` dibaca dari zip (tanpa python-docx).
2. Paragraf `w:p` menjadi satu baris; `w:t` teksnya, `w:tab` → tab, `w:br`/`w:cr` → newline.
3. Tabel `w:tbl`: setiap baris `w:tr` menjadi satu baris teks, sel `w:tc` digabung dengan tab —
   hasilnya siap ditempel ke markdown/TSV. Urutan paragraf dan tabel dipertahankan.
4. Teks dipotong 60.000 karakter; `chars` selalu berisi jumlah karakter asli, `note` menjelaskan
   kalau ekstraksi gagal.

`xlsx` membaca `xl/sharedStrings.xml` + `xl/worksheets/sheet*.xml` (nama sheet ikut), `pptx`
membaca `ppt/slides/slide*.xml` per slide. Semuanya *best effort*: file rusak tidak membuat server
crash.

### Cara teks dikirim ke agent

```
[Lampiran dari pengguna]
1. laporan.docx (application/vnd...wordprocessingml.document) — 1.1 KB, path: /root/.hermes/webchat/uploads/2026-10-24/03e5cd8e_laporan.docx
2. foto.png (image/png) — 73 B, path: /root/.hermes/webchat/uploads/2026-10-24/9ff24fb6_foto.png

[Image attached at: /root/.hermes/webchat/uploads/2026-10-24/9ff24fb6_foto.png]
Catatan gambar: … panggil tool vision_analyze dengan image_url berisi path absolut gambar tersebut …

===== ISI laporan.docx =====
Laporan Proyek Merapi 2026
Total anggaran tahun ini adalah Rp 1.250.000.000 ...
===== AKHIR laporan.docx =====
```

### Endpoint

`POST /api/upload` — wajib sesi + pemeriksaan `Origin`/`Referer`. Body `multipart/form-data`,
satu field bernama `file` (body mentah `image/*` juga masih diterima untuk kompatibilitas).

```bash
curl -b jar -X POST http://127.0.0.1:8650/api/upload -F file=@laporan.docx
```

| Status | Arti |
|---|---|
| `200` | `{id, name, mime, size, bytes, path, url, kind, preview, text, truncated, chars, note, ok}` |
| `400` | boundary/body tidak valid, atau file kosong |
| `401` | belum login |
| `403` | `Origin`/`Referer` beda host |
| `413` | lebih dari 25 MB |
| `415` | ekstensi/isi berkas tidak didukung |

`kind` = `"image"` atau `"text"`. `path` absolut di server (dipakai agent), `url` =
`/api/uploads/<id>`, `preview` data URL hanya untuk gambar ≤ 2 MB.

`GET /api/uploads/<id>` — menyajikan lampiran (thumbnail setelah reload). Wajib sesi; `id` 8 hex;
path kanonik diverifikasi di dalam `uploads/`; `nosniff` + `Content-Security-Policy: sandbox`.

`GET /api/image?path=<absolut>` — endpoint lama, tetap hidup untuk transkrip lama. Path hanya
boleh di bawah `/root/.hermes/cache/webchat-images/`, `/root/.hermes/cache/images/`, atau
`uploads/` webchat ini. Cache gambar lama (`/root/.hermes/cache/webchat-images/`) dibersihkan
otomatis setelah 14 hari; `uploads/` tidak di-prune otomatis (`rm -rf uploads/2026-*`).

### Keamanan

- Nama file disanitasi: basename, NFKD → ASCII, karakter aneh → `_`.
  `../../etc/passwd.docx` tersimpan sebagai `passwd.docx`.
- File tidak pernah dieksekusi — hanya dibaca sebagai teks atau disajikan sebagai gambar/PDF.
- `uploads/` ada di `.gitignore`; isi dokumen tidak disimpan di localStorage browser (hanya
  metadata chip: id, nama, ukuran, jumlah karakter).

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

Papan Kanban + Workspace (cookie yang sama):

```bash
curl -s -b jar http://127.0.0.1:8650/agents | grep -c 'id="papan"'            # 1
curl -s -b jar http://127.0.0.1:8650/api/agents/boards                       # daftar board
curl -s -b jar 'http://127.0.0.1:8650/api/agents/board?slug=pjtki'           # counts + tasks
curl -s -b jar 'http://127.0.0.1:8650/api/agents/board?slug=../../etc'       # 400 (ditolak)
curl -s -b jar http://127.0.0.1:8650/workspace | grep -c 'id="workspace-room"'  # 1
curl -s -b jar 'http://127.0.0.1:8650/api/agents/live?slug=pjtki'            # kantor virtual (JSON)
curl -s -b jar http://127.0.0.1:8650/api/agents/assignees                    # profil worker
curl -s -b jar -X POST http://127.0.0.1:8650/api/agents/task-create \
  -H 'Origin: http://127.0.0.1:8650' -H 'Content-Type: application/json' \
  -d '{"slug":"pjtki","title":"Cek dapur","assignee":"default","priority":2}'  # 201
curl -s -b jar 'http://127.0.0.1:8650/api/agents/dispatch-preview?slug=pjtki'  # dry-run dispatcher
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

### Manajemen sesi (buat · ganti judul · fork · hapus)

Panel **Riwayat** bukan lagi hanya baca. Tiap baris sesi punya empat aksi, dan tombol
**+ Sesi** di kepala panel membuat sesi Hermes baru (langsung dipakai sebagai `session_id`
percakapan berikutnya):

| Tombol | Endpoint | Catatan |
|---|---|---|
| **+ Sesi** | `POST /api/sessions` | Membuat baris sesi kosong di state Hermes, judul otomatis `Sesi webchat <waktu>`. |
| **Ganti judul** | `PATCH /api/sessions/{id}` `{title}` | Modal internal berisi input (bukan `prompt()`); judul percakapan lokal yang menunjuk sesi itu ikut diperbarui. |
| **Fork** | `POST /api/sessions/{id}/fork` `{}` | Sama seperti `/branch` di CLI: transkrip disalin ke sesi baru, dan **sesi asal ditandai berakhir (`branched`)**. Judul fork mengikuti penomoran lineage (`… #2`). |
| **Hapus** | `DELETE /api/sessions/{id}` | Konfirmasi modal internal. Menghapus sesi **di server** (transkrip, tool call, gambar); percakapan di browser tidak ikut terhapus. |

Tombol **Lanjutkan chat** tetap seperti sebelumnya (pesan baru masuk ke sesi yang dibaca).
Semua aksi memakai `fetch` yang sama dengan halaman ini, jadi cookie 401 tetap memicu
re-login, dan POST/PATCH/DELETE tetap melewati cek Origin + rate limit mutasi.

### Arahan saat run berjalan (steer)

Selama run berjalan muncul kotak **Arahan** di atas composer. Isi teks lalu Enter (atau
tombol **Arahan**) → `POST /v1/runs/{run_id}/steer` `{input}`. Teks itu disisipkan ke agent
yang sedang bekerja, bukan menjadi pesan baru, dan muncul sebagai chip `↪ …` di transkrip.
Event `run.steered` dari stream menandai chip itu sudah diterima (kalau event datang lebih
dulu, chip baru dibuat dari event). Kalau run sudah tidak menerima steer, server menjawab
`409 run_not_accepting_steer` dan chip menampilkan alasan penolakannya.

Diuji nyata: satu run yang sedang menunggu jawaban klarifikasi diberi arahan
"sebut juga kata 'steer-ok'", dan jawaban akhir agent memang memuat kata itu.

### Klarifikasi (agent bertanya balik)

Kalau tool `clarify` aktif, agent bisa bertanya di tengah run. Webchat menampilkan kartu
biru **"Agen menanyakan sesuatu"** berisi pertanyaan, tombol pilihan dari agent, dan kolom
jawaban bebas:

1. Stream mengirim `clarify.request` `{prompt_id, execution_generation, question, choices[], multi_select}`.
2. Klik pilihan atau tulis jawaban → `POST /v1/runs/{run_id}/clarify`
   `{request_id, execution_generation, answer}` — **tepat tiga field itu**; server menolak
   field lain (`invalid_params`) dan generasi yang basi (`stale_generation`).
3. Server membalas `{run_id, status: "resolved", prompt_id}` lalu mengirim `clarify.settled`.

> **Penting — toolset `clarify` harus dipasang untuk platform `api_server`.**
> Default `hermes-api-server` sengaja **tidak** memuat `clarify` (postur "coding tanpa UI
> interaktif"). Tanpa itu, agent di jalur `/v1/runs` tidak punya tool untuk bertanya balik,
> jadi kartu ini tidak akan pernah muncul. Di mesin ini `~/.hermes/config.yaml` karena itu
> memuat satu baris tambahan di luar repo:
>
> ```yaml
> platform_toolsets:
>   # clarify: kartu tanya-jawab di webchat (lihat README webchat bagian Klarifikasi).
>   # Tanpa toolset ini, agen di platform api_server tidak bisa bertanya balik.
>   api_server: [hermes-api-server, clarify]
> ```
>
> Kalau baris itu dihapus, agent kembali tidak bisa bertanya balik (kartu klarifikasi mati,
> fitur lain tidak terpengaruh). Mapping ini dibaca **per run** (`_get_platform_tools(...)`
> saat agent dibuat), jadi menambah/menghapusnya berlaku pada run berikutnya **tanpa
> restart gateway** — dibuktikan dengan memicu `clarify.request` tanpa me-restart apa pun.
> Backup config sebelum perubahan: `/root/.hermes/config.yaml.bak-clarify-20261024`
> (salinan config tanpa blok `clarify`; `cp` balik lalu hapus baris itu untuk kembali).

### Unduhan artifact

Path artifact yang muncul di teks agent (`/v1/artifacts/download/<id>`, mis. dari
`download_path` pada receipt upload) diubah menjadi tautan **⇩ artifact** yang menunjuk
path yang sama, sehingga unduhannya lewat origin webchat (cookie sesi ikut, API key tetap di
server). Proxy meneruskan `Content-Type`, `Content-Disposition`, `X-Artifact-Sha256`, dan
`X-Artifact-Id` apa adanya. Artifact gateway bersifat **sekali pakai** (unduhan kedua `404`)
dan kedaluwarsa ±5 menit, jadi tautan tidak di-prefetch.

Upload artifact untuk agent tersedia di `POST /v1/artifacts/upload` (badan mentah,
`Content-Type` wajib ada di allowlist gateway, nama berkas lewat `X-Artifact-Filename`).

> Endpoint artifact hanya hidup kalau `browser.extension_control.enabled: true` di
> config Hermes; default-nya `false` → gateway menjawab `404 browser_control_disabled`.
> Proxy meneruskan status itu apa adanya. (Uji 200 + `Content-Disposition` di repo ini
> dilakukan dengan flag dinyalakan sementara, lalu dikembalikan ke `false`.)

### Model per sesi

Panel **Setelan → Model untuk sesi ini** mengunci model untuk satu sesi Hermes:
`POST /api/sessions/{id}/model` `{model, provider}`. Klik **Pilih model sesi…** membuka
pemilih model yang sama, tapi pilihannya dikirim ke endpoint sesi (banner pemilih berubah
jadi "target: sesi …") dan **model default profil tidak berubah** — header chat tetap
menampilkan model profil. Kunci sesi menang atas pilihan model di header untuk sesi itu.
Status kunci dibaca dari `GET /api/sessions/{id}` (`has_model_config` + `model`).

## Papan Kanban (`/agents`)

Halaman kedua: **papan kanban yang bisa ditulis** — bukan cuma memantau. Tujuh kolom
(**Triage · Todo · Ready · Running · Review · Blocked · Done**), kartu bisa ditarik antar
kolom, task baru dibuat dari modal, dan setiap aksi dikerjakan oleh CLI `hermes kanban`
yang sama dengan yang dipakai agent. Mobile-first: kolom bisa digeser horizontal, target
sentuh ≥ 36 px, tanpa CDN/dependency (satu file HTML).

Dibuka lewat tautan **Papan** di header chat/workspace (dan ditandai `aria-current` di
halaman ini). Cookie sesi yang sama berlaku; tanpa sesi → 302 ke `/login`.

### Cara pakai

1. **Buat task** — tombol `+ Task`. Isi judul (wajib, ≤200), deskripsi, assignee (dari
   `kanban assignees`), prioritas 0–5, workspace (`scratch` default, `worktree`, atau
   folder yang sudah ada), dan centang **Triage** kalau spesifikasinya belum jelas.
   Task langsung muncul sebagai kartu; tanpa Triage ia masuk `Ready`.
2. **Assign** — pilih profil worker. Hanya ada satu profil (`default`) di pemasangan ini;
   worker dijalankan oleh profil tersebut. Tambah profil di luar UI dengan
   `hermes profile create <nama>`.
3. **Tarik kartu** antar kolom (desktop) atau pakai tombol **Pindah kolom** di detail
   (sentuh). Peta transisinya:

   | Tarik ke | Dari | Aksi CLI |
   |---|---|---|
   | Ready | Todo, Blocked | `promote` |
   | Review | Ready | `request-review` |
   | Ready | Review | `reopen-review` |
   | Blocked | Ready/Todo/Review | `block --kind <…>` (alasan wajib, lewat modal) |
   | Done | Ready/Todo/Review/Blocked | `complete --result <…>` |
   | Done | Running | `complete --force` (konfirmasi ekstra: run worker ditutup) |
   | Triage/Todo | apa pun | **ditolak** dengan penjelasan (Triage hanya untuk task baru; Todo = menunggu induk) |
   | apa pun | Running | **ditolak** dari drag — pakai tombol di detail agar run worker ikut ditutup |

   Drop yang tidak masuk akal **selalu** dijawab pesan (toast), bukan gagal diam-diam;
   penolakan dari CLI ditampilkan apa adanya (mis. "unsatisfied parent dependencies").
4. **Promote = worker jalan otomatis.** `promote` memindahkan task ke `Ready`; dispatcher
   yang berjalan **di dalam gateway** (bukan di proses webchat ini) akan meng-claim dan
   men-spawn worker pada tick berikutnya (±60 detik). Tidak ada loop dispatch di server
   ini, dan `kanban dispatch` tanpa `--dry-run` tidak pernah dipanggil dari UI. Tombol
   **Pratinjau dispatch** hanya menjalankan `dispatch --dry-run --json` (read-only).
5. **Detail sheet** (klik kartu) memuat semua yang lama — status, PID/heartbeat, workspace,
   body, komentar, event, run — plus aksi: Assign, Set model, Promote, Block, Unblock,
   Complete, Request review, Reopen review, Komentar, Edit judul/body, Arsipkan
   (arsip = keluar dari papan, **bukan** hapus; tidak ada tombol hapus permanen).
   Aksi destruktif (arsip, complete paksa) memakai dialog konfirmasi internal.
6. **Board** — pemilih board + tombol **Board baru** (slug + nama). Board baru punya
   queue/workspace sendiri dan tidak mengubah board aktif global (`kanban switch`).

Semua dialog dibuat sendiri (`.modalWrap`) — tidak memakai `confirm()`/`prompt()` bawaan
browser, supaya bisa memuat beberapa field, tervalidasi, dan bergaya sama di mobile.

Chip ringkasan tetap ada (`Running n`, `Ready n`, …): ketuk = filter, ketuk lagi = lepas.
Indikator kanan atas tetap "diperbarui N dtk lalu" dengan titik hijau/merah; auto-refresh
10 detik hanya saat tab terlihat.

### Endpoint baca (lokal, bukan proxy)

Dibaca langsung dari SQLite kanban (mode `ro`), tidak diteruskan ke gateway. Tetap wajib
sesi login + same-origin (Origin/Referer beda host → 403).

| Endpoint | Isi |
|---|---|
| `GET /api/agents/boards` | `[{slug, name, description, task_count, running_count, current}]` |
| `GET /api/agents/board?slug=<slug>&limit=<n>` | `{board, generated_at, counts, tasks, has_db, returned, truncated}`, `limit` 1–200 |
| `GET /api/agents/task?slug=<slug>&id=<task_id>` | satu task lengkap: `body`, `comments`, `events`, `runs` |
| `GET /api/agents/live?slug=<slug>` | state per profil untuk halaman Workspace |
| `GET /api/agents/assignees` | `{assignees:[{name,on_disk,counts}], count, single, note, generated_at}` — dari `kanban assignees --json`, cache 30 dtk |
| `GET /api/agents/dispatch-preview?slug=<slug>` | `{board, generated_at, preview}` — `kanban --board <slug> dispatch --dry-run --json` |

`tasks[]`: `id, title, status, assignee, priority, created_at, started_at, completed_at,
worker_pid, worker_alive, last_heartbeat_at, workspace_path, workspace_kind, branch_name,
block_kind, body_preview, body_truncated, comment_count, last_comment, last_event, parents,
children`. Body penuh hanya lewat `/api/agents/task`.

### Endpoint tulis (baru)

Semua `POST` wajib: sesi login (401 tanpa cookie), `Origin`/`Referer` sama dengan `Host`
(403 kalau beda), dan body `application/json` (bukan JSON → 400). Rate limit 40 mutasi /
60 detik per IP+pengguna (429 + pesan). Mutasi **tidak pernah** menulis `kanban.db`
langsung — selalu subprocess `hermes kanban`.

**`POST /api/agents/task-create`** → `201`

```json
{"slug":"proyek","title":"Buat hello.txt","body":"…","assignee":"default",
 "priority":2,"workspace":"scratch","triage":false,"parent":"t_1a2b3c4d"}
```

`title` wajib ≤200 (tidak boleh diawali `-`), `body` ≤20000, `assignee` harus ada di
`kanban assignees` atau `"none"`, `priority` 0–5, `workspace` = `scratch` |
`worktree` | `worktree:/abs/path` | `dir:/abs/path`, `triage` opsional, `parent` opsional
(anak dari task terbuka lahir di `Todo`). Respons:

```json
{"ok":true,"action":"create","board":"proyek","id":"t_1a2b3c4d","status":"ready",
 "task":{…},"message":"Task t_1a2b3c4d dibuat (status ready)."}
```

**`POST /api/agents/task-update`** → `200`

```json
{"slug":"proyek","id":"t_1a2b3c4d","action":"block",
 "reason":"butuh kredensial staging","kind":"needs_input"}
```

| `action` | Field | CLI |
|---|---|---|
| `assign` | `assignee` (nama profil atau `"none"`) | `assign <id> <profil>` |
| `promote` | `reason?` | `promote <id> [reason]` |
| `block` | `reason` (wajib ≤500), `kind?` (`needs_input`/`capability`/`dependency`/`transient`) | `block --kind=<k> <id> -- <alasan>` |
| `unblock` | `reason?` | `unblock --reason=<r> <id>` |
| `complete` | `result?` ≤4000, `force?` | `complete --result=<r> [--force] <id>` |
| `archive` | – | `archive <id>` |
| `review` | `summary?`, `reviewer?`, `force?` | `request-review --summary=<s> <id>` |
| `reopen` | `reason?` | `reopen-review --reason=<r> <id>` |
| `comment` | `text` (wajib ≤8000) | `comment <id> -- <teks>` |
| `edit` | `title?`/`body?`/`priority?` (min satu) | `edit <id> --title=… --body=… --priority=…` |
| `set-model` | `model` (atau `"none"`), `provider?` | `set-model [--provider=<p>] <id> <model>` |

Respons memuat `{ok, action, board, id, status, previous_status, task, message}`;
`message` adalah keluaran CLI apa adanya (mis. `Blocked t_…: alasan`). Kode galat:
400 validasi, 403 origin, 404 board/task tidak ada, 409 CLI menolak transisi (mis.
`promote` saat induk belum selesai, `complete` tanpa bukti hasil), 429 rate limit,
502/503 CLI rusak/tidak ada, 504 timeout (60 dtk).

**`POST /api/agents/board-create`** → `201`

```json
{"slug":"proyek-baru","name":"Proyek Baru"}
```

Slug `^[a-z0-9][a-z0-9_-]{0,63}$`; nama ≤80 (default = Title Case slug). Board dibuat
tanpa mengubah board aktif global. Duplikat → 409.

### Keamanan & alasan desain

- **Kenapa lewat CLI, bukan tulis DB.** Status bukan sekadar kolom: `promote` menandai task
  siap di-claim, `complete` menutup run worker, `block` mencatat `block_kind` + event, dan
  dispatcher gateway memakai invariant yang sama (parent dependency, anti-loop block,
  claim/lock). `INSERT/UPDATE` langsung akan melewati semua itu — papan tampak berubah tapi
  worker tidak pernah dijadwalkan, dan event/hook lifecycle hilang.
- **argv list, tanpa `shell=True`.** Nilai dari klien masuk sebagai satu argumen; flag
  tidak pernah berasal dari klien (klien hanya memilih `action` dari whitelist, server yang
  memetakan ke flag tetap). Nilai yang diawali `-` dipisah dengan `--` atau dikirim sebagai
  `--flag=nilai`; judul yang diawali `-` ditolak 400.
- **Env child disusun eksplisit** (`HERMES_HOME`, `HOME`, `PATH`, `LANG`, `PYTHONIOENCODING`)
  — tidak ada API key/sesi gateway yang diwarisi proses anak.
- **Validasi ketat**: slug & id task regex, assignee harus ada di daftar profil, priority
  integer 0–5, panjang judul/body/komentar/alasan dibatasi, `action` whitelist, `kind`
  whitelist, path workspace absolut tanpa `..`.
- **Timeout & stderr terpotong**: subprocess dibatasi 60 detik (504 kalau lewat), stderr
  dipotong 600 karakter dan di-scrub (`api_key=`, `token=`, `sk-…`) sebelum dikirim ke
  browser. Handler menangkap semua exception → tidak ada request yang menggantung.
- Setiap panggilan CLI butuh ±4 detik (startup Python), jadi UI menampilkan status sibuk dan
  pesan hasil; kegagalan CLI muncul sebagai toast berisi pesan aslinya.

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

## Otomasi (`/otomasi`) — cron, skills, toolsets, status

Halaman keempat: satu tempat untuk hal-hal yang biasanya hanya terlihat di CLI. Empat tab,
mobile-first, satu file tanpa CDN/dependency. Dibuka lewat navigasi **Otomasi** di header
semua halaman (Chat · Kanban · Workspace · Otomasi, `aria-current` mengikuti halaman aktif)
dan pintasan **Otomasi** di sidebar chat. Cookie sesi sama; tanpa sesi → 302 ke `/login`.

### Tab Cron — tugas terjadwal

Daftar job dari `GET /api/jobs?include_disabled=true`. Tiap kartu menampilkan nama, status
(aktif/dijeda/selesai), jadwal (`schedule_display`), jalan berikutnya, **terakhir jalan +
status hasil**, skills, dan cuplikan prompt. Aksi: **Jeda/Lanjutkan**, **Jalankan sekarang**,
**Hasil terakhir**, **Ubah**, **Hapus** (konfirmasi modal internal).

Form **+ Job baru** mengirim `POST /api/jobs` dengan `name`, `schedule`, `prompt`, `deliver`,
dan `skills` opsional. `schedule` menerima durasi (`30m`, `2h`), cron 5 kolom
(`0 9 * * *`), atau frasa `every …`; tombol preset mengisi contohnya. **Ubah** memakai
`PATCH /api/jobs/{id}` (field di-whitelist gateway: `name`, `schedule`, `prompt`, `deliver`,
`skills`, `repeat`, `enabled`).

Job dijalankan **gateway**, bukan tab ini, jadi setelah **Jalankan sekarang** status baru
berubah ketika run selesai (bisa ~1 menit); halaman memantau sendiri beberapa kali.
Badan hasil run tidak dikirim `GET /api/jobs`, jadi webchat membaca berkas yang memang
ditulis cron — `GET /api/jobs/{id}/output` → `$HERMES_HOME/cron/output/<id>/*.md` (bagian
`## Response` saja, dibatasi 20 KB). Endpoint ini lokal (bukan proxy) dan id-nya wajib 12 hex.

Bukti uji yang dipakai saat membangun fitur ini: job dibuat lewat halaman → `hermes cron list`
menampilkannya, `pause` → `[paused]` di CLI, `resume` → `[active]`, `PATCH` judul → nama di
CLI ikut berubah, `run` → `last_status: ok` + berkas hasil, `DELETE` → CLI kembali kosong.

### Tab Skills

`GET /v1/skills` (read-only — halaman ini **tidak** memasang/menghapus skill), dengan kotak
cari atas nama/deskripsi/kategori dan pengelompokan per kategori.

> Build 0.22.1 di mesin ini mengembalikan **500** untuk `GET /v1/skills`
> (`TypeError: _find_all_skills() got an unexpected keyword argument 'include_editorial'`).
> Karena itu webchat punya cadangan: kalau gateway menjawab ≥500, daftar dibaca dari disk
> (`$HERMES_HOME/skills/<kategori>/<nama>/SKILL.md`, frontmatter `name`/`description`) dengan
> bentuk JSON yang sama, dan responsnya ditandai `"source": "local-fallback"` + `note`.
> Tab Skills menampilkan label **"dari disk (cadangan)"** supaya jelas asalnya, bukan pura-pura
> datang dari gateway. Setelah bug upstream diperbaiki, jalur gateway otomatis dipakai lagi.

### Tab Toolsets

`GET /v1/toolsets` — tiap toolset platform `api_server`: label, nama, deskripsi, daftar tool
konkret, plus chip **aktif/nonaktif** dan **terkonfigurasi/butuh kunci**. Read-only; untuk
mengubahnya sunting `platform_toolsets` di config Hermes (lihat bagian Klarifikasi).

### Tab Status

Menggabungkan `GET /health/detailed` + `GET /v1/capabilities` + fakta lokal dalam satu
endpoint `GET /api/status`:

- **Gateway**: versi, state, pid, jumlah agen aktif.
- **Model aktif**: provider · model dari `GET /api/model/options` (profil default).
- **Uptime**: proses gateway (dihitung dari `/proc/<pid>` + `/proc/uptime`) dan uptime
  proses webchat — gateway tidak mengirim uptime di endpoint mana pun.
- **Kesiapan**: chip per cek (`state_db`, `session_store`, `config`, `model`, `disk`,
  `gateway`, `background_queues`) beserta `used_percent`/state-nya.
- **Kanban**: jumlah board + ukuran `kanban.db` (dibaca dari direktori board lokal).
- **state.db**: path + ukuran + status terbaca.
- **Permintaan hari ini** + p95 dari `api_server.metrics.metrics_today`.
- **Capabilities**: tabel fitur (`true`/`false`/objek) dan tabel endpoint yang diiklankan.

### Kontrak endpoint baru (semua di balik sesi login)

| Endpoint webchat | Diteruskan ke gateway | Catatan |
|---|---|---|
| `GET /api/jobs` | `GET /api/jobs` | Daftar job; query `include_disabled` ikut. |
| `GET /api/jobs/{id}` | `GET /api/jobs/{id}` | id wajib 12 hex, kalau tidak `404` di router. |
| `POST /api/jobs` | `POST /api/jobs` | Body wajib JSON objek; bukan JSON → `400` lokal. |
| `PATCH /api/jobs/{id}` | `PATCH /api/jobs/{id}` | Whitelist field ditegakkan gateway. |
| `DELETE /api/jobs/{id}` | `DELETE /api/jobs/{id}` | Tanpa body. |
| `POST /api/jobs/{id}/pause\|resume\|run` | sama | Body opsional (`run` boleh `{prompt}`). |
| `GET /api/jobs/{id}/output` | **lokal** | Hasil run terakhir dari `cron/output`. |
| `POST /api/cron/fire` | `POST /api/cron/fire` | Diteruskan, tapi butuh JWT Chronos (bukan API key) → UI tidak memakainya. |
| `GET /v1/skills` | `GET /v1/skills` | Ada cadangan lokal (lihat di atas). |
| `GET /v1/toolsets` | `GET /v1/toolsets` | Read-only. |
| `GET /health/detailed` | `GET /health/detailed` | Sekarang `401` JSON tanpa sesi (bukan 302). |
| `GET /v1/capabilities` | `GET /v1/capabilities` | Read-only. |
| `GET /api/status` | **lokal** | Gabungan health+capabilities+uptime+kanban+db. |
| `POST /api/sessions` | `POST /api/sessions` | Buat sesi. |
| `PATCH /api/sessions/{id}` | `PATCH /api/sessions/{id}` | Ganti judul. |
| `DELETE /api/sessions/{id}` | `DELETE /api/sessions/{id}` | Hapus sesi. |
| `POST /api/sessions/{id}/fork` | sama | Fork (= `/branch`). |
| `POST /api/sessions/{id}/model` | sama | Kunci model per sesi. |
| `POST /v1/runs/{id}/steer` | sama | Sudah ada jalurnya (proxy `/v1/runs/*`), sekarang dipakai UI. |
| `POST /v1/runs/{id}/clarify` | sama | idem; body `{request_id, execution_generation, answer}`. |
| `POST /v1/artifacts/upload` | sama | Badan mentah + `Content-Type` + `X-Artifact-Filename`. |
| `GET /v1/artifacts/download/{id}` | sama | Biner + `Content-Disposition`/`X-Artifact-*`. |

Pengaman yang berlaku untuk semuanya: sesi login wajib (`401` JSON untuk `/api/*`, `/v1/*`,
`/health/*`), POST/PATCH/DELETE wajib same-origin (`403`), id divalidasi regex sebelum
ditempel ke URL gateway, body JSON diperiksa lokal (`400`), ukuran body dibatasi, dan mutasi
(`POST /api/jobs`, `PATCH/DELETE`, `POST /api/sessions`, fork, model, upload artifact) kena
rate limit yang sama dengan kanban (`WEBCHAT_MUTATE_LIMIT`, default 40/menit → `429`).
Path gateway **tidak** dibuka borongan: hanya daftar di tabel ini yang punya rute; mis.
`/api/secrets` dan `/v1/models` tetap `404`.

## File

| File | Isi |
|---|---|
| `server.py` | Server + reverse proxy + login + unggah/ekstraksi lampiran (stdlib Python saja) + endpoint `/api/agents/*` + proxy Otomasi (`/api/jobs`, `/v1/skills`, `/v1/toolsets`, `/health/detailed`, `/v1/capabilities`, sesi, artifact) |
| `static/index.html` | Seluruh UI chat (HTML + CSS + JS, tanpa CDN): sidebar percakapan, lampiran, lightbox |
| `static/agents.html` | Halaman **Papan Kanban** (`/agents`) — papan 7 kolom yang bisa ditulis (drag/aksi via CLI), mobile-first |
| `static/workspace.html` | Halaman **Workspace** (`/workspace`) — kantor virtual realtime (sprite SVG + animasi CSS) |
| `static/otomasi.html` | Halaman **Otomasi** (`/otomasi`) — tab Cron (job terjadwal), Skills, Toolsets, Status sistem |
| `static/login.html` | Halaman login |
| `uploads/` | Lampiran terunggah (gambar + dokumen), dibuat otomatis, `0700`, **tidak** di-commit |
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

Halaman **Papan Kanban** (`/agents`) dan **Workspace** (`/workspace`) tidak lewat gateway:
`server.py` membaca file SQLite kanban Hermes langsung (read-only) dan memeriksa
`/proc/<pid>` untuk liveness, jadi keduanya tetap bisa dibuka walau API server sedang mati —
yang tidak jalan hanya halaman chat-nya. Khusus **mutasi** papan (buat task, promote, block,
complete, komentar, board baru), `server.py` memanggil CLI `hermes kanban` sebagai subprocess
(lihat bagian *Papan Kanban → Keamanan*); dispatcher yang men-spawn worker tetap milik
gateway, bukan proses webchat.

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
