import json
import os
import sys
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import yfinance as yf

# Określ katalog docelowy dla plików JSON i raportów
TARGET_DIRECTORY = "./pobrane_dane"


class DownloadWorker(QThread):
  finished = pyqtSignal(str, str, bool)

  def __init__(self, ticker, period, interval, file_path):
    super().__init__()
    self.ticker = ticker
    self.period = period
    self.interval = interval
    self.file_path = file_path

  def run(self):
    try:
      df = yf.download(
          self.ticker,
          period=self.period,
          interval=self.interval,
          progress=False,
      )
      if df.empty:
        self.finished.emit(self.ticker, "Brak danych od yfinance", False)
        return

      data_json = df.to_json(orient="split", date_format="iso")
      parsed = json.loads(data_json)

      os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
      with open(self.file_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=4)

      self.finished.emit(self.ticker, self.file_path, True)
    except Exception as e:
      self.finished.emit(self.ticker, str(e), False)


class BulkDownloadWorker(QThread):
  finished = pyqtSignal(str, bool)

  def __init__(self, tickers_map, ranges, target_dir):
    super().__init__()
    self.tickers_map = tickers_map
    self.ranges = ranges
    self.target_dir = target_dir

  def run(self):
    try:
      os.makedirs(self.target_dir, exist_ok=True)
      for label_name, yf_ticker in self.tickers_map.items():
        safe_name = label_name.replace("/", "_").replace("!", "")
        for suffix, period, interval in self.ranges:
          file_name = f"{safe_name}_{suffix}.json"
          file_path = os.path.join(self.target_dir, file_name)
          
          df = yf.download(yf_ticker, period=period, interval=interval, progress=False)
          if not df.empty:
            data_json = df.to_json(orient="split", date_format="iso")
            parsed = json.loads(data_json)
            with open(file_path, "w", encoding="utf-8") as f:
              json.dump(parsed, f, ensure_ascii=False, indent=4)
              
      self.finished.emit("Pobieranie wsadowe zakończone sukcesem!", True)
    except Exception as e:
      self.finished.emit(f"Błąd pobierania wsadowego: {str(e)}", False)


class MainWindow(QMainWindow):

  def __init__(self):
    super().__init__()
    self.setWindowTitle("Pobieracz Danych Yahoo Finance i Generator Raportów (Qt)")
    self.resize(850, 750)

    self.tickers_map = {
        "NQ1!": "NQ=F",
        "ni225": "^N225",
        "USD/JPY": "USDJPY=X",
        "DXY": "DX-Y.NYB",
        "HSTECH": "HSTECH.HK",
        "HSI": "^HSI",
        "CL1!": "CL=F",
        "GC1!": "GC=F",
        "000001.SS": "000001.SS",
        "ES1!": "ES=F",
        "DJI": "^DJI",
        "SOXX/SMH": "SOXX",
        "SNDK": "SNDK",
        "NVDA": "NVDA",
    }

    self.ranges = [
        ("miesiac", "1mo", "1d"),
        ("tydzien", "7d", "1h"),
        ("dzien", "2d", "5m"),
    ]

    self.init_ui()
    self.workers = []

  def init_ui(self):
    central_widget = QWidget()
    main_layout = QVBoxLayout(central_widget)

    info_label = QLabel(
        f"Katalog zapisu plików: <b>{os.path.abspath(TARGET_DIRECTORY)}</b>"
    )
    main_layout.addWidget(info_label)

    global_layout = QHBoxLayout()
    self.btn_all_month = QPushButton("Pobierz WSZYSTKO: Miesiąc (1d)")
    self.btn_all_month.clicked.connect(lambda: self.start_bulk_download([("miesiac", "1mo", "1d")]))
    global_layout.addWidget(self.btn_all_month)

    self.btn_all_week = QPushButton("Pobierz WSZYSTKO: Tydzień (1h)")
    self.btn_all_week.clicked.connect(lambda: self.start_bulk_download([("tydzien", "7d", "1h")]))
    global_layout.addWidget(self.btn_all_week)

    self.btn_all_day = QPushButton("Pobierz WSZYSTKO: Dzień (5m)")
    self.btn_all_day.clicked.connect(lambda: self.start_bulk_download([("dzien", "2d", "5m")]))
    global_layout.addWidget(self.btn_all_day)

    self.btn_all_everything = QPushButton("Pobierz WSZYSTKO (Wszystkie zakresy)")
    self.btn_all_everything.clicked.connect(lambda: self.start_bulk_download(self.ranges))
    global_layout.addWidget(self.btn_all_everything)

    main_layout.addLayout(global_layout)

    self.btn_generate_report = QPushButton("📄 Generuj jednolity raport syntetyczny dla AI")
    self.btn_generate_report.clicked.connect(self.generate_ai_report_file)
    main_layout.addWidget(self.btn_generate_report)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll_content = QWidget()
    grid_layout = QGridLayout(scroll_content)

    grid_layout.addWidget(QLabel("<b>Ticker</b>"), 0, 0)
    grid_layout.addWidget(QLabel("<b>Miesiąc (1d)</b>"), 0, 1)
    grid_layout.addWidget(QLabel("<b>Tydzień (1h)</b>"), 0, 2)
    grid_layout.addWidget(QLabel("<b>Dzień (5m)</b>"), 0, 3)

    row = 1
    for label_name, yf_ticker in self.tickers_map.items():
      grid_layout.addWidget(QLabel(label_name), row, 0)

      for col_idx, (range_suffix, period, interval) in enumerate(
          self.ranges, start=1
      ):
        btn = QPushButton(f"Pobierz {range_suffix}")
        btn.clicked.connect(
            lambda checked, t=yf_ticker, l=label_name, p=period, i=interval, s=
            range_suffix: self.start_download(t, l, p, i, s)
        )
        grid_layout.addWidget(btn, row, col_idx)

      row += 1

    scroll.setWidget(scroll_content)
    main_layout.addWidget(scroll)

    self.status_label = QLabel("Gotowy do pracy.")
    main_layout.addWidget(self.status_label)

    self.setCentralWidget(central_widget)

  def start_download(self, yf_ticker, label_name, period, interval, suffix):
    safe_name = label_name.replace("/", "_").replace("!", "")
    file_name = f"{safe_name}_{suffix}.json"
    file_path = os.path.join(TARGET_DIRECTORY, file_name)

    self.status_label.setText(
        f"Pobieranie danych dla {label_name} ({suffix})..."
    )

    worker = DownloadWorker(yf_ticker, period, interval, file_path)
    worker.finished.connect(self.on_download_finished)
    self.workers.append(worker)
    worker.start()

  def start_bulk_download(self, ranges_to_fetch):
    self.status_label.setText("Trwa masowe pobieranie danych dla wszystkich tickerów...")
    worker = BulkDownloadWorker(self.tickers_map, ranges_to_fetch, TARGET_DIRECTORY)
    worker.finished.connect(self.on_bulk_finished)
    self.workers.append(worker)
    worker.start()

  def on_download_finished(self, ticker, result_info, success):
    self.workers = [w for w in self.workers if w.isRunning()]
    display_name = next(
        (k for k, v in self.tickers_map.items() if v == ticker), ticker
    )
    if success:
      self.status_label.setText(f"Sukces! Zapisano plik: {os.path.basename(result_info)}")
    else:
      self.status_label.setText(f"Błąd dla {display_name}: {result_info}")

  def on_bulk_finished(self, message, success):
    self.workers = [w for w in self.workers if w.isRunning()]
    self.status_label.setText(message)

  def _calculate_pct_change(self, file_path):
    if not os.path.exists(file_path):
      return "Brak pliku"
    try:
      with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        columns = data.get("columns", [])
        rows = data.get("data", [])
        
        close_idx = -1
        for idx, col in enumerate(columns):
          if isinstance(col, list) and len(col) > 0 and col[0] == 'Close':
            close_idx = idx
            break
          elif isinstance(col, str) and col == 'Close':
            close_idx = idx
            break

        if close_idx == -1 and len(columns) > 3:
          close_idx = 3

        if rows and close_idx != -1 and len(rows[0]) > close_idx:
          first_close = rows[0][close_idx]
          last_close = rows[-1][close_idx]
          
          if first_close is not None and last_close is not None:
            change_pct = ((last_close - first_close) / first_close) * 100
            return f"Cena: {last_close:.2f} ({change_pct:+.2f}%)"
    except Exception:
      return "Błąd odczytu"
    return "Brak danych"

  def generate_ai_report_file(self):
    if not os.path.exists(TARGET_DIRECTORY):
      self.status_label.setText("Brak folderu z danymi! Najpierw pobierz dane.")
      return

    market_lines = []

    for label_name in self.tickers_map.keys():
      safe_name = label_name.replace("/", "_").replace("!", "")
      
      month_summary = self._calculate_pct_change(os.path.join(TARGET_DIRECTORY, f"{safe_name}_miesiac.json"))
      week_summary = self._calculate_pct_change(os.path.join(TARGET_DIRECTORY, f"{safe_name}_tydzien.json"))
      day_summary = self._calculate_pct_change(os.path.join(TARGET_DIRECTORY, f"{safe_name}_dzien.json"))

      metric_summary = (
          f"- **{label_name}** | 🟢 Miesiąc (1d): {month_summary} | 🟡 Tydzień (1h): {week_summary} | 🔴 Dzień (5m): {day_summary}"
      )
      market_lines.append(metric_summary)

    report_content = f"""# RAPORT RYNKOWY DLA AI (Aktualny stan wszystkich instrumentów)

*Zestawienie wielookresowe (Miesiąc / Tydzień / Dzień) dla wszystkich śledzonych aktywów:*

""" + "\n".join(market_lines) + f"""

---
### Instrukcja dla modelu AI:
Przeanalizuj powyższe dane wielookresowe dla wszystkich instrumentów jako jedną spójną całość:
1. Oceń ogólny stan i nastroje na globalnym rynku.
2. Sprawdź spójność trendów między poszczególnymi interwałami czasowymi (miesiąc, tydzień, dzień).
3. Wskaż najważniejsze korelacje i anomalie pomiędzy indeksami, surowcami, walutami i spółkami.
"""

    report_path = os.path.join(TARGET_DIRECTORY, "raport_dla_ai.md")
    try:
      with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)
      self.status_label.setText(f"Wygenerowano jednolity raport AI: {report_path}")
    except Exception as e:
      self.status_label.setText(f"Błąd zapisu raportu: {str(e)}")


if __name__ == "__main__":
  app = QApplication(sys.argv)
  app.setStyleSheet("QWidget { font-size: 16pt; }")
  window = MainWindow()
  window.show()
  sys.exit(app.exec_())