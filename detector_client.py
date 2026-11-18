"""
Contoh skrip AI eksternal: jalankan YOLO di perangkat lain (PC GPU / Jetson) lalu kirim
hasilnya ke dashboard Flask lewat POST /api/detections.

    pip install ultralytics opencv-python requests
    python detector_client.py --source rtsp://... --model models/best.pt --server http://192.168.1.5:5000 --key <API_KEY>

API key ada di menu Pengaturan > Integrasi AI eksternal.
Set URL streaming kamera di dashboard ke "demo" TIDAK perlu; cukup biarkan, data dari skrip ini
akan menimpa hasil deteksi. Jika kamera hanya dibaca oleh skrip ini, isi URL di dashboard dengan
alamat kamera yang sama atau biarkan gambar dikirim lewat field "image".
"""
import argparse
import base64
import time

import cv2
import requests


def kind_of(name: str):
    n = name.lower()
    if any(k in n for k in ("berserakan", "scatter", "litter", "luar", "outside")):
        return "berserakan"
    if any(k in n for k in ("penuh", "full", "overflow")):
        return "penuh"
    if any(k in n for k in ("kosong", "empty")):
        return "kosong"
    if any(k in n for k in ("sebagian", "half", "partial", "bin")):
        return "sebagian"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    ap.add_argument("--model", default="models/best.pt")
    ap.add_argument("--server", default="http://localhost:5000")
    ap.add_argument("--key", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--conf", type=float, default=0.5)
    a = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(a.model)
    cap = cv2.VideoCapture(int(a.source) if a.source.isdigit() else a.source)
    while True:
        ok, frame = cap.read()
        if not ok:
            print("Kamera tidak terbaca, mencoba lagi...")
            time.sleep(3)
            cap = cv2.VideoCapture(int(a.source) if a.source.isdigit() else a.source)
            continue
        h, w = frame.shape[:2]
        res = model.predict(frame, conf=a.conf, verbose=False)[0]
        boxes = []
        for b in res.boxes:
            k = kind_of(res.names[int(b.cls)])
            if k:
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                boxes.append({"kind": k, "conf": float(b.conf), "box": [x1 / w, y1 / h, x2 / w, y2 / h]})
        _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        try:
            r = requests.post(f"{a.server}/api/detections", headers={"X-API-Key": a.key}, timeout=10,
                              json={"boxes": boxes, "image": base64.b64encode(jpg).decode()})
            print(r.status_code, [(b["kind"], round(b["conf"], 2)) for b in boxes])
        except requests.RequestException as e:
            print("Gagal kirim:", e)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
