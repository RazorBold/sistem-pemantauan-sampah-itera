"""
Menyiapkan dataset YOLO-seg dari ekspor Label Studio di folder samplefoto/.

Masalah yang diperbaiki:
1. Label campur: box_sampah = poligon, sampah_plastik = kotak (cx cy w h).
   YOLO akan salah membaca jika dicampur, jadi kotak diubah menjadi poligon persegi panjang.
2. Foto HP 4000x3000 (sebagian diputar lewat EXIF) -> diputar tegak & diperkecil (sisi panjang 1280 px).
3. Foto diambil beruntun (selisih detik) -> dibagi train/val/test per BLOK waktu,
   bukan acak, agar foto yang hampir kembar tidak muncul di latih sekaligus uji.

Jalankan:  python siapkan_dataset.py
Hasil   :  dataset/{images,labels}/{train,val,test} + dataset/data.yaml
Foto asli di samplefoto/ tidak diubah.
"""
import os
import re
import shutil
import sys

from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "samplefoto")
DST = os.path.join(BASE, "dataset")
SISI_PANJANG = 1280
BLOK = 8            # jumlah foto berurutan dalam satu blok waktu
POLA = "TTTVTTX"    # T = train, V = val, X = test (diulang per blok) -> kira-kira 70/15/15
NAMA_KELAS = ["box_sampah", "sampah_plastik"]


def waktu_foto(nama):
    """Kunci urutan waktu dari nama file Label Studio: <hash>-IMG_20260914_151700.jpg / <hash>-2026-09-14_00.31.41.jpg"""
    asli = nama.split("-", 1)[1]
    m = re.search(r"IMG_(\d{8})_(\d{6})", asli)
    if m:
        return ("A", m.group(1) + m.group(2))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})\.(\d{2})\.(\d{2})", asli)
    if m:
        return ("B", "".join(m.groups()))
    return ("Z", asli)


def ubah_label(baris):
    """Satu baris label -> baris YOLO-seg (kelas + titik poligon)."""
    v = baris.split()
    if not v:
        return None
    kelas, angka = int(v[0]), [min(1.0, max(0.0, float(x))) for x in v[1:]]
    if len(angka) == 4:  # kotak cx cy w h -> poligon 4 titik
        cx, cy, w, h = angka
        x1, y1, x2, y2 = max(0, cx - w / 2), max(0, cy - h / 2), min(1, cx + w / 2), min(1, cy + h / 2)
        angka = [x1, y1, x2, y1, x2, y2, x1, y2]
    if len(angka) < 6 or len(angka) % 2:
        return None
    return f"{kelas} " + " ".join(f"{a:.6f}" for a in angka)


def main():
    if not os.path.isdir(os.path.join(SRC, "images")):
        sys.exit("Folder samplefoto/images tidak ditemukan.")
    nama = sorted(os.listdir(os.path.join(SRC, "images")), key=waktu_foto)
    nama = [n for n in nama if n.lower().endswith((".jpg", ".jpeg", ".png"))]
    if os.path.isdir(DST):
        shutil.rmtree(DST)
    for bag in ("train", "val", "test"):
        os.makedirs(os.path.join(DST, "images", bag))
        os.makedirs(os.path.join(DST, "labels", bag))

    kode = {"T": "train", "V": "val", "X": "test"}
    hitung = {"train": [0, 0, 0], "val": [0, 0, 0], "test": [0, 0, 0]}  # foto, bak, plastik
    for i, n in enumerate(nama):
        bag = kode[POLA[(i // BLOK) % len(POLA)]]
        dasar = os.path.splitext(n)[0]
        f_lbl = os.path.join(SRC, "labels", dasar + ".txt")
        if not os.path.isfile(f_lbl):
            print("  lewati (tanpa label):", n)
            continue
        im = ImageOps.exif_transpose(Image.open(os.path.join(SRC, "images", n))).convert("RGB")
        im.thumbnail((SISI_PANJANG, SISI_PANJANG), Image.LANCZOS)
        im.save(os.path.join(DST, "images", bag, dasar + ".jpg"), quality=92)
        baris = [b for b in (ubah_label(x) for x in open(f_lbl, encoding="utf-8")) if b]
        with open(os.path.join(DST, "labels", bag, dasar + ".txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(baris) + "\n")
        hitung[bag][0] += 1
        hitung[bag][1] += sum(b.startswith("0 ") for b in baris)
        hitung[bag][2] += sum(b.startswith("1 ") for b in baris)
        if (i + 1) % 50 == 0 or i + 1 == len(nama):
            print(f"  {i + 1}/{len(nama)} foto diproses")

    with open(os.path.join(DST, "data.yaml"), "w", encoding="utf-8") as f:
        f.write(f"path: {DST.replace(os.sep, '/')}\ntrain: images/train\nval: images/val\ntest: images/test\n"
                f"names:\n" + "".join(f"  {i}: {k}\n" for i, k in enumerate(NAMA_KELAS)))
    for bag, (nf, nb, np_) in hitung.items():
        print(f"  {bag:5}: {nf:3} foto | {nb:3} box_sampah | {np_:4} sampah_plastik")
    print("Selesai ->", os.path.join(DST, "data.yaml"))


if __name__ == "__main__":
    main()
