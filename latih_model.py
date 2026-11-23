"""
Melatih model YOLOv8-seg untuk 2 kelas: box_sampah (bak bata) & sampah_plastik.

Langkah:
  1. python siapkan_dataset.py     (sekali, membuat folder dataset/)
  2. python latih_model.py         (training; GPU dipakai otomatis jika ada)
Hasil terbaik disalin ke models/best.pt dan langsung dipakai notebook & web.
"""
import os
import shutil

import torch
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "dataset", "data.yaml")

EPOCHS = 80        # berapa kali seluruh foto dipelajari ulang
IMGSZ = 640        # ukuran gambar saat training
BATCH = 8          # cocok untuk GPU 4 GB; turunkan ke 4 jika "out of memory"
MODEL_AWAL = "yolov8s-seg.pt"   # model pralatih COCO (diunduh otomatis ~23 MB)


def main():
    if not os.path.isfile(DATA):
        raise SystemExit("dataset/data.yaml belum ada. Jalankan dulu: python siapkan_dataset.py")
    device = 0 if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    print("Perangkat training:", torch.cuda.get_device_name(0) if device == 0 else device)

    model = YOLO(os.path.join(BASE, "models", MODEL_AWAL) if os.path.isfile(
        os.path.join(BASE, "models", MODEL_AWAL)) else MODEL_AWAL)
    model.train(data=DATA, epochs=EPOCHS, imgsz=IMGSZ, batch=BATCH, device=device,
                patience=25, workers=2, seed=0, deterministic=False,
                # mask tiap objek disimpan terpisah. Bawaan (True) membuat mask bak "bolong" di posisi
                # plastik, karena mask kecil menimpa mask besar yang tumpang-tindih
                overlap_mask=False,
                project=os.path.join(BASE, "runs"), name="sampah_seg", exist_ok=True,
                plots=True, verbose=False)

    best = os.path.join(BASE, "runs", "sampah_seg", "weights", "best.pt")
    os.makedirs(os.path.join(BASE, "models"), exist_ok=True)
    shutil.copy(best, os.path.join(BASE, "models", "best.pt"))
    print("Model terbaik disalin ke models/best.pt")

    # Evaluasi jujur pada data UJI (tidak pernah dilihat saat training)
    m = YOLO(os.path.join(BASE, "models", "best.pt"))
    r = m.val(data=DATA, split="test", imgsz=IMGSZ, batch=BATCH, device=device, overlap_mask=False,
              project=os.path.join(BASE, "runs"), name="sampah_seg_test", exist_ok=True, verbose=True)
    print(f"\nTEST  box mAP50={r.box.map50:.3f}  mAP50-95={r.box.map:.3f}  "
          f"mask mAP50={r.seg.map50:.3f}  P={r.box.mp:.3f}  R={r.box.mr:.3f}")


if __name__ == "__main__":
    main()
