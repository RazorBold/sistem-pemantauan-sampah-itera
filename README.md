# Sistem Pemantauan Sampah Cerdas – Kampus ITERA

Web dashboard berbasis Flask untuk memantau tempat sampah lewat CCTV + AI (YOLO), lengkap dengan
notifikasi WhatsApp, riwayat deteksi, laporan PDF/Excel, dan pengaturan. Semua berjalan lokal.

## Menjalankan

```bash
pip install -r requirements.txt
python app.py
```

Buka http://localhost:5000, lalu login dengan **admin / admin123** (segera ganti di Pengaturan > Akun).

OpenCV (`cv2`) **tidak wajib**. Tanpa OpenCV, web tetap jalan penuh di mode Simulasi dan eksternal
(gambar diolah dengan Pillow). OpenCV baru dibutuhkan untuk webcam, CCTV RTSP, file video, dan model YOLO:
`pip install opencv-python` (nama paketnya bukan `cv2`).

Database SQLite dibuat otomatis di `data/sampah.db`. Snapshot bukti deteksi disimpan di `data/snapshots/`.

Ingin mencoba halaman Laporan dengan data 14 hari? Jalankan:
`flask --app app seed-demo` (data contoh, jangan dipakai untuk hasil penelitian).

## Sumber kamera (Pengaturan > Kamera > URL streaming)

| Nilai | Arti |
|---|---|
| `demo` | Mode simulasi (bawaan). Memakai gambar contoh dan skenario kosong → penuh → berserakan |
| `0` | Webcam laptop |
| `rtsp://user:pass@IP:554/...` | IP camera / CCTV RTSP |
| `C:\video\uji.mp4` | File video (diputar berulang) |
| `eksternal` | Hasil deteksi dikirim oleh `detector_client.py` lewat API |

### Menu Simulasi

Saat URL streaming = `demo`, CCTV menampilkan foto-foto di menu **Simulasi**.

Skenario **Deteksi AI** memakai foto bak bata ITERA, dan hasilnya dihitung langsung oleh model `models/best.pt` (sama dengan notebook):
- poligon bak
- plastik di dalam bak (kotak hijau tipis)
- plastik di luar bak (berserakan)
- level = % luas bak yang tertutup plastik

Status bak ditentukan dari level dan ambang di Pengaturan (bawaan: penuh ≥ 50%, kosong ≤ 10%). Foto unggahan juga bisa
diberi kondisi "Deteksi AI". Skenario tong umum yang lama tetap ada, tetapi tidak ikut putaran (bisa dinyalakan lagi).

Fitur menu Simulasi:
- **Putar otomatis**: ganti skenario setiap 10–60 detik (penuh, kosong, berserakan, cahaya redup, CCTV offline).
- **Tampilkan**: kunci satu skenario untuk demo/uji tertentu.
- **Tambah skenario**: unggah foto sendiri, lalu pilih kondisi dan kepenuhannya. Kotak deteksi dibuat otomatis sesuai kondisi.
- Setiap deteksi baru memunculkan alert pop-up di halaman admin mana pun (dengan bunyi setelah halaman diklik sekali),
  dan mengirim WhatsApp sesuai aturan notifikasi.

Jika kamera tidak bisa dibaca, dashboard menampilkan **CCTV offline**. Kondisi ini dicatat sebagai
kejadian `offline` dan tidak dihitung sebagai kondisi sampah.

## Model AI (YOLO)

1. `pip install ultralytics`
2. Taruh model hasil training di `models/best.pt` (path bisa diubah di Pengaturan > Deteksi AI).
3. Nama kelas dikenali dari kata kunci: `penuh/full`, `kosong/empty`, `berserakan/scattered/litter`,
   `sebagian/half/bin`.

Persentase kepenuhan diestimasi dari kelas + confidence (`estimate_fullness()` di `app.py`). Jika
penelitian memakai metode lain (segmentasi, regresi tinggi tumpukan), ganti fungsi tersebut.

Alur di `Monitor._process()`:
1. Buang deteksi dengan confidence di bawah ambang.
2. Hitung sampah berserakan hanya jika berada di dalam ROI.
3. Kondisi harus sama selama *N frame stabil* sebelum dicatat. Ini mengurangi false alert.
4. Jika kondisi stabil berubah, sistem menyimpan snapshot, mencatat `detection_logs`, dan membuat `cleaning_actions` (untuk penuh/berserakan).
5. Kirim WhatsApp jika aturan notifikasi aktif dan cooldown sudah lewat.
6. Penanganan otomatis ditandai selesai saat sistem mendeteksi tong kembali tidak penuh atau area kembali bersih.

## WhatsApp

Pilih di Pengaturan > WhatsApp API:

- **Simulasi**: tidak mengirim apa pun, tetapi tercatat di riwayat. Cocok untuk uji lokal.
- **WhatsApp Business Cloud API (Meta)**: isi Access token + Phone Number ID. Status *delivered/read*
  masuk lewat webhook `/webhook/whatsapp`, yang butuh URL publik, misalnya ngrok. Pesan teks bebas hanya bisa
  dikirim dalam jendela 24 jam. Di luar itu Meta mewajibkan *message template*.
- **Fonnte**: isi token dari fonnte.com. Pilihan paling sederhana untuk nomor Indonesia.

Setiap alert penuh/berserakan berisi link konfirmasi `/konfirmasi/<token>`. Petugas membukanya dari HP
untuk menandai sampah sudah ditangani, tanpa perlu login. Isi **Alamat dashboard** dengan IP laptop
(mis. `http://192.168.1.5:5000`) agar link bisa dibuka dari jaringan yang sama.

## Notebook deteksi (`deteksi_sampah.ipynb`)

Notebook terpisah dari web untuk menguji alur deteksi sesuai diagram alir penelitian:
webcam/folder → input citra → preprocessing → resize & normalisasi → backbone → neck (fusi fitur) →
detection head → bounding box + kelas + confidence + mask → NMS → valid? → hitung level → kategori
(kosong/sebagian/penuh/berserakan) → tampil di web → simpan data → WhatsApp.

Model: **YOLOv8s-seg** yang dilatih sendiri (`models/best.pt`) dengan 2 kelas hasil pelabelan Label Studio:
`box_sampah` (poligon bak bata) dan `sampah_plastik` (kotak).

- **Level (%)** = luas bak yang tertutup plastik ÷ luas bak.
- Plastik di luar bak = **berserakan**.

```bash
pip install ultralytics notebook matplotlib pandas
jupyter notebook deteksi_sampah.ipynb
```

- Foto uji: `dataset/images/test/` (40 foto, tidak dipakai saat training), lengkap dengan labelnya untuk evaluasi.
- Hasil uji saat ini:
  - Deteksi: box mAP50 0,975 dan mask mAP50 0,975.
  - Level: selisih rata-rata 1,8 poin persen dibanding level dari label asli.
  - Status bak benar 87,5%, berserakan benar 90%.
- Hasil disimpan di `notebook_output/` (CSV monitoring + gambar beranotasi).
- `KIRIM_KE_WEB = True` mengirim hasil ke dashboard (set URL streaming CCTV = `eksternal`).

### Menjalankan di Google Colab

1. Siapkan paket `TA_Sampah_Colab.zip`. Isinya: notebook, model, dataset lengkap, dan skrip training.
2. Ekstrak zip, lalu unggah folder `TA_Sampah` ke **Google Drive → My Drive**.
3. Buka `deteksi_sampah.ipynb` dengan Google Colaboratory.
4. Pilih **Runtime → Change runtime type → T4 GPU**, lalu **Run all**.

Sel "0. Khusus Google Colab" akan menyambungkan Drive dan memasang ultralytics secara otomatis.
Mode webcam dan kirim ke web tidak bisa dipakai dari Colab.

### Melatih ulang model (misalnya setelah menambah foto berlabel)

1. Taruh ekspor Label Studio (format YOLO: `images/`, `labels/`, `classes.txt`) di `samplefoto/`.
2. `python siapkan_dataset.py`. Langkah ini memutar dan mengecilkan foto, menyeragamkan label, lalu membagi train/val/test per blok waktu.
3. `python latih_model.py`. GPU dipakai otomatis; di RTX 3050 Ti makan waktu sekitar 25 menit. Hasilnya disalin ke `models/best.pt`.

GPU NVIDIA di Windows butuh torch versi CUDA:
`pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126`

## Struktur

```
app.py                 backend Flask (auth, worker CCTV+AI, WhatsApp, API, laporan)
detector_client.py     contoh skrip YOLO eksternal -> POST /api/detections
templates/             halaman (Jinja2)
static/css, static/js  tampilan & pembaruan real-time (polling /api/state tiap 2,5 detik)
static/vendor          Chart.js (lokal, tanpa internet)
models/                taruh best.pt di sini
data/                  database SQLite + snapshot
```

Tabel database mengikuti PRD: `users`, `cameras`, `detection_logs`, `notification_recipients`,
`notification_logs`, `system_settings`, `cleaning_actions`, ditambah `fullness_samples` untuk grafik tren.
