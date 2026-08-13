import glob
import os
import cv2
import numpy as np
import pandas as pd
import pytesseract
from datetime import datetime, timedelta
import re

# Konfiguracja ścieżki Tesseract (odkomentuj i dostosuj w razie potrzeby na Windowsie)
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


def setup_directories():
    """Tworzy katalogi wejściowe i wyjściowe."""
    os.makedirs('./surowe', exist_ok=True)
    os.makedirs('./przetworzone', exist_ok=True)


def ocr_digits_only(crop_img):
    """Odczytuje datę/czas z wyciętego fragmentu osi X."""
    if crop_img is None or crop_img.size == 0:
        return ''

    # Powiększenie 3x dla poprawy ostrości OCR
    resized = cv2.resize(crop_img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    bordered = cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=[0, 0, 0])

    # Konfiguracja Tesseract - tylko cyfry i separatory czasowe
    custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789-.:,$B/ '
    text = pytesseract.image_to_string(bordered, config=custom_config).strip()
    return text


def get_volume_scale(filename):
    """
    Zwraca zakres wolumenu (Min, Max w Mld USD) dla lewej osi Y
    w zależności od nazwy pliku.
    """
    fname_lower = filename.lower()

    if 'btc' in fname_lower:
        return 0.0, 10.0   # Zakres wolumenu dla BTC (0.0 - 10.0 Mld)
    elif 'sol' in fname_lower:
        return 0.0, 5.0    # Zakres wolumenu dla SOL (0.0 - 5.0 Mld)
    else:
        return 0.0, 10.0   # Domyślna skala uniwersalna


def normalize_csv(file_path):
    """
    Normalizuje datę w CSV – generuje format: YYYY-MM-DD, HH:MM:SS
    """
    df = pd.read_csv(file_path)
    basename = os.path.basename(file_path)

    match = re.search(r'_(\d+)h_', basename)
    interval_hours = int(match.group(1)) if match else 1

    n_rows = len(df)
    if n_rows == 0:
        return

    end_time = datetime.now().replace(second=0, microsecond=0)

    times = []
    for i in range(n_rows):
        offset_hours = (n_rows - 1 - i) * interval_hours
        t = end_time - timedelta(hours=offset_hours)
        # Format z przecinkiem między datą a czasem
        times.append(t.strftime('%Y-%m-%d, %H:%M:%S'))

    df['Data i Czas'] = times
    df.to_csv(file_path, index=False)
    print(f'  [✓] Znormalizowano czas (data, czas) i zapisano: {file_path}')


def process_image(file_path):
    filename = os.path.basename(file_path)
    print(f'\n[+] Processing: {filename}')

    img = cv2.imread(file_path)
    if img is None:
        print(f'[-] Błąd podczas wczytywania pliku: {filename}')
        return

    h, w, _ = img.shape
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Pobranie skali wolumenu lewej osi Y dla danej waluty
    vol_min, vol_max = get_volume_scale(filename)

    # Wyznaczenie pionowych granic obszaru wykresu (Lewa oś Y)
    y_top = int(h * 0.10)     # Górna linia siatki (Max Vol)
    y_bottom = int(h * 0.85)  # Dolna linia bazowa słupków (Min Vol = 0)

    def pixel_to_volume(y_pos):
        """Przelicza pozycję Y szczytu słupka na wolumen (Mld USD)."""
        y_clamped = max(y_top, min(y_bottom, y_pos))
        ratio = (y_bottom - y_clamped) / float(y_bottom - y_top)
        return vol_min + ratio * (vol_max - vol_min)

    # Maska dla słupków wolumenu (zielone/jasne słupki na ciemnym tle)
    lower_green = np.array([30, 30, 40])
    upper_green = np.array([90, 255, 255])
    mask_green = cv2.inRange(hsv, lower_green, upper_green)

    contours, _ = cv2.findContours(mask_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bars = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        # Filtrujemy szumy i małe artefakty – bierzemy tylko rzeczywiste słupki wolumenu
        if bh > 10 and bw >= 3 and bh < (h * 0.80):
            bars.append((x, y, bw, bh))

    # Sortowanie słupków po osi X (od lewej do prawej)
    bars = sorted(bars, key=lambda b: b[0])

    results = []
    for idx, (bx, by, bw, bh) in enumerate(bars):
        # Szczyt słupka przeliczony na lewą oś Y
        vol_val = pixel_to_volume(by)

        # Wycięcie fragmentu z osią czasu pod dolną krawędzią słupka
        crop_y1 = min(h - 5, by + bh)
        crop_y2 = min(h, crop_y1 + int(h * 0.08))
        crop_x1 = max(0, bx - 8)
        crop_x2 = min(w, bx + bw + 8)

        time_crop = img[crop_y1:crop_y2, crop_x1:crop_x2]
        time_str = ocr_digits_only(time_crop)

        results.append({
            'Index': idx + 1,
            'Czas (OCR)': time_str if time_str else 'N/A',
            'Wolumen (Mld USD - Lewa Oś Y)': round(vol_val, 3)
        })

    # Tworzenie DataFrame i zapis
    df = pd.DataFrame(results)
    base_name = os.path.splitext(filename)[0]
    csv_output_path = f'./przetworzone/{base_name}_dane.csv'
    df.to_csv(csv_output_path, index=False)
    print(f'  [✓] Wykryto słupków wolumenu: {len(bars)}. Zapisano tymczasowy CSV.')

    # Normalizacja daty (format: YYYY-MM-DD, HH:MM:SS)
    normalize_csv(csv_output_path)


def main():
    setup_directories()

    extensions = ('*.png', '*.jpg', '*.jpeg')
    files = []
    for ext in extensions:
        files.extend(glob.glob(os.path.join('./surowe', ext)))

    if not files:
        print('[-] Brak plików w katalogu ./surowe! Wklej tam obrazy wykresów.')
        return

    print(f'Znaleziono {len(files)} plików do przetworzenia.')
    for file_path in files:
        process_image(file_path)

    print('\n=== PRZETWARZANIE ZAKOŃCZONE USPECHNIE ===')


if __name__ == '__main__':
    main()