import glob
import os
import cv2
import numpy as np
import pandas as pd
import pytesseract
from datetime import datetime, timedelta
import re

# Konfiguracja ścieżki Tesseract (odkomentuj i ustaw jeśli wymagane na Twoim systemie Windows)
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


def setup_directories():
    """Tworzy katalogi wejściowe i wyjściowe, jeśli nie istnieją."""
    os.makedirs('./surowe', exist_ok=True)
    os.makedirs('./przetworzone', exist_ok=True)


def ocr_digits_only(crop_img):
    """Maksymalnie dokładny OCR dla cyfr i symboli daty/czasu."""
    if crop_img is None or crop_img.size == 0:
        return ''

    # 1. Powiększenie (3x)
    resized = cv2.resize(crop_img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    # 2. Szarość + Binaryzacja Otsu
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 3. Dodanie marginesu wokół ramki
    bordered = cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=[0, 0, 0])

    # 4. Konfiguracja Tesseract dla cyfr i separatorów
    custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789-.:,$B/ '
    text = pytesseract.image_to_string(bordered, config=custom_config).strip()
    return text


def get_crypto_scales(filename):
    """
    Zwraca dynamiczny zakres skali na podstawie nazwy pliku (BTC vs SOL).
    W razie potrzeby możesz łatwo dostosować zakresy min/max poniżej.
    """
    fname_lower = filename.lower()
    
    # Domyślne wartości skali dla Bitcoin (BTC)
    if 'btc' in fname_lower:
        vol_min, vol_max = 0.0, 10.0   # Mld USD
        price_min, price_max = 60000.0, 100000.0 # USD
    # Domyślne wartości dla Solana (SOL)
    elif 'sol' in fname_lower:
        vol_min, vol_max = 0.0, 5.0    # Mld USD
        price_min, price_max = 100.0, 300.0      # USD
    else:
        # Skala uniwersalna/domyślna
        vol_min, vol_max = 0.0, 10.0
        price_min, price_max = 0.0, 1000.0

    return vol_min, vol_max, price_min, price_max


def normalize_csv(file_path):
    """
    Normalizuje kolumnę czasu w pliku CSV.
    Generuje daty rozdzielone PRZECINKIEM (YYYY-MM-DD, HH:MM:SS).
    """
    df = pd.read_csv(file_path)
    basename = os.path.basename(file_path)
    
    match = re.search(r'_(\d+)h_', basename)
    if not match:
        # Domyślnie 1h jeśli brak oznaczenia w nazwie
        interval_hours = 1
    else:
        interval_hours = int(match.group(1))

    n_rows = len(df)
    if n_rows == 0:
        return

    end_time = datetime.now().replace(second=0, microsecond=0)

    times = []
    for i in range(n_rows):
        offset_hours = (n_rows - 1 - i) * interval_hours
        t = end_time - timedelta(hours=offset_hours)
        # Zapis daty i czasu oddzielonych po przecinku
        times.append(t.strftime('%Y-%m-%d, %H:%M:%S'))

    df['Data i Czas'] = times

    df.to_csv(file_path, index=False)
    print(f'  [✓] Znormalizowano czas (data, czas) i zapisano: {file_path}')


def process_image(file_path):
    filename = os.path.basename(file_path)
    print(f'\n[+] Processing: {filename}')

    img = cv2.imread(file_path)
    if img is None:
        print(f'[-] Błąd podczas wczytywania: {filename}')
        return

    h, w, _ = img.shape
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Pobieramy dopasowane skale cenowe i wolumenowe dla danego aktywa (BTC/SOL)
    vol_min, vol_max, price_min, price_max = get_crypto_scales(filename)

    # Wyrównanie osi Y na podstawie wysokości obrazka
    y_top = int(h * 0.08)
    y_bottom = int(h * 0.88)

    def pixel_to_volume(y):
        y_clamped = max(y_top, min(y_bottom, y))
        ratio = (y_bottom - y_clamped) / float(y_bottom - y_top)
        return vol_min + ratio * (vol_max - vol_min)

    def pixel_to_price(y):
        y_clamped = max(y_top, min(y_bottom, y))
        ratio = (y_bottom - y_clamped) / float(y_bottom - y_top)
        return price_min + ratio * (price_max - price_min)

    # Wykrywanie zielonych słupków
    lower_green = np.array([30, 40, 40])
    upper_green = np.array([90, 255, 255])
    mask_green = cv2.inRange(hsv, lower_green, upper_green)

    contours, _ = cv2.findContours(mask_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bars = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        # Filtracja artefaktów – wykrywamy rzeczywiste słupki
        if bh > 12 and bw >= 3 and bh < (h * 0.85):
            bars.append((x, y, bw, bh))

    # Sortowanie słupków od lewej do prawej
    bars = sorted(bars, key=lambda b: b[0])

    # Maskowanie żółtej linii ceny
    lower_yellow = np.array([15, 80, 100])
    upper_yellow = np.array([38, 255, 255])
    mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)

    results = []

    for idx, (bx, by, bw, bh) in enumerate(bars):
        center_x = bx + bw // 2
        
        # Wolumen liczony od szczytu słupka
        vol_val = pixel_to_volume(by)

        # Wykrywanie ceny na linii Y w punkcie X słupka
        yellow_pixels = np.where(mask_yellow[:, max(0, center_x - 1):min(w, center_x + 2)] > 0)[0]
        if len(yellow_pixels) > 0:
            line_y = np.mean(yellow_pixels)
            price_val = pixel_to_price(line_y)
        else:
            price_val = None

        # Dynamiczny obszar OCR poniżej słupka
        crop_y1 = min(h - 5, by + bh)
        crop_y2 = min(h, crop_y1 + int(h * 0.08))
        crop_x1 = max(0, bx - 6)
        crop_x2 = min(w, bx + bw + 6)

        time_crop = img[crop_y1:crop_y2, crop_x1:crop_x2]
        time_str = ocr_digits_only(time_crop)

        results.append({
            'Index': idx + 1,
            'Czas (OCR)': time_str if time_str else 'N/A',
            'Słupek (Vol Mld USD)': round(vol_val, 3),
            'Linia Cena (USD)': round(price_val, 2) if price_val else None,
        })

    # Zapis i normalizacja
    df = pd.DataFrame(results)
    base_name = os.path.splitext(filename)[0]
    csv_output_path = f'./przetworzone/{base_name}_dane.csv'
    df.to_csv(csv_output_path, index=False)
    print(f'  [✓] Odnaleziono słupków: {len(bars)}. Zapisano: {csv_output_path}')

    normalize_csv(csv_output_path)


def main():
    setup_directories()

    extensions = ('*', '*.jpg', '*.jpeg')
    files = []
    for ext in extensions:
        files.extend(glob.glob(os.path.join('./surowe', ext)))

    if not files:
        print('[-] Brak plików w katalogu ./surowe! Wrzuć tam obrazki wykresów.')
        return

    print(f'Znaleziono {len(files)} plików do przetworzenia.')
    for file_path in files:
        process_image(file_path)

    print('\n=== WSZYSTKIE PLIKI ZOSTAŁY PRZETWORZONE ===')


if __name__ == '__main__':
    main()