import json
import os
import re
import sys
from datetime import datetime

import pandas as pd
import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIRECTORY = os.path.join(BASE_DIR, "pobrane_dane")


def safe_label(label_name: str) -> str:
    """
    Tworzy bezpieczną nazwę pliku na podstawie etykiety instrumentu.

    Przykłady:
    NQ1! -> NQ1
    USD/JPY -> USD_JPY
    SOXX/SMH -> SOXX_SMH
    """
    label_name = str(label_name).replace("/", "_").replace("!", "")
    label_name = re.sub(r"[^A-Za-z0-9._-]", "_", label_name)
    label_name = re.sub(r"_+", "_", label_name).strip("_")
    return label_name or "instrument"


def make_file_path(target_dir: str, label_name: str, suffix: str) -> str:
    """
    Jedna wspólna funkcja do budowania ścieżki pliku JSON.
    Używana przy pobieraniu pojedynczym, wsadowym i generowaniu raportu.
    """
    return os.path.join(target_dir, f"{safe_label(label_name)}_{suffix}.json")


def normalize_dataframe(df):
    """
    Spłaszcza kolumny MultiIndex z yfinance do zwykłych nazw,
    np. ['Close', 'BTC-USD'] -> 'Close'.
    """
    if df is None:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    return df


def download_ohlcv(ticker: str, period: str, interval: str):
    """
    Pobiera dane z yfinance z bezpiecznym fallbackiem,
    gdyby starsza wersja biblioteki nie obsługiwała auto_adjust/prepost.
    """
    try:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            prepost=False,
        )
    except TypeError:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
        )

    return normalize_dataframe(df)


class DownloadWorker(QThread):
    download_finished = pyqtSignal(str, str, bool)

    def __init__(self, ticker: str, period: str, interval: str, file_path: str):
        super().__init__()
        self.ticker = ticker
        self.period = period
        self.interval = interval
        self.file_path = file_path

    def run(self):
        try:
            df = download_ohlcv(self.ticker, self.period, self.interval)

            if df is None or df.empty:
                self.download_finished.emit(self.ticker, "Brak danych od yfinance", False)
                return

            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

            data_json = df.to_json(orient="split", date_format="iso")
            parsed = json.loads(data_json)

            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=4)

            self.download_finished.emit(self.ticker, self.file_path, True)

        except Exception as e:
            self.download_finished.emit(self.ticker, str(e), False)


class BulkDownloadWorker(QThread):
    bulk_finished = pyqtSignal(str, bool)
    progress = pyqtSignal(str)

    def __init__(self, tickers_map: dict, ranges: list, target_dir: str):
        super().__init__()
        self.tickers_map = tickers_map
        self.ranges = ranges
        self.target_dir = target_dir

    def run(self):
        try:
            os.makedirs(self.target_dir, exist_ok=True)

            errors = []
            saved_files = 0
            total_tasks = len(self.tickers_map) * len(self.ranges)
            done_tasks = 0

            for label_name, yf_ticker in self.tickers_map.items():
                for suffix, period, interval in self.ranges:
                    done_tasks += 1
                    self.progress.emit(
                        f"Pobieranie {label_name} / {suffix} ({done_tasks}/{total_tasks})..."
                    )

                    file_path = make_file_path(self.target_dir, label_name, suffix)

                    try:
                        df = download_ohlcv(yf_ticker, period, interval)

                        if df is None or df.empty:
                            errors.append(f"{label_name}/{suffix}: brak danych")
                            continue

                        data_json = df.to_json(orient="split", date_format="iso")
                        parsed = json.loads(data_json)

                        with open(file_path, "w", encoding="utf-8") as f:
                            json.dump(parsed, f, ensure_ascii=False, indent=4)

                        saved_files += 1

                    except Exception as e:
                        errors.append(f"{label_name}/{suffix}: {str(e)}")

            if errors:
                message = f"Zapisano {saved_files} plików. Problemy: " + "; ".join(errors[:8])
                if len(errors) > 8:
                    message += f" (+{len(errors) - 8} kolejnych)"
                self.bulk_finished.emit(message, False)
            else:
                self.bulk_finished.emit(
                    f"Pobieranie wsadowe zakończone. Zapisano {saved_files} plików.",
                    True,
                )

        except Exception as e:
            self.bulk_finished.emit(f"Błąd pobierania wsadowego: {str(e)}", False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pobieracz Danych Yahoo Finance i Generator Raportów (Qt)")
        self.resize(1200, 850)

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
            # Nowe instrumenty krypto i powiązane:
            "SOL-USD": "SOL-USD",
            "SOL-BTC": "SOL-BTC",
            "BTC-USD": "BTC-USD",
            "ETH-USD": "ETH-USD",
            "COIN": "COIN",
            "MSTR": "MSTR",
        }

        self.crypto_tickers_map = {
            "SOL-USD": "SOL-USD",
            "SOL-BTC": "SOL-BTC",
            "BTC-USD": "BTC-USD",
            "ETH-USD": "ETH-USD",
            "COIN": "COIN",
            "MSTR": "MSTR",
        }

        # Celowo dzień jest pobierany jako 1d, żeby raportować ostatni dzień,
        # a nie dwa dni jak w poprzedniej wersji.
        self.ranges = [
            ("miesiac", "1mo", "1d"),
            ("tydzien", "7d", "1h"),
            ("dzien", "1d", "5m"),
        ]

        self.workers = []
        self.ticker_checkboxes = {}

        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)

        info_label = QLabel(
            f"Katalog zapisu plików: <b>{os.path.abspath(TARGET_DIRECTORY)}</b><br>"
            "Po zmianie nazewnictwa plików pobierz dane od nowa."
        )
        info_label.setWordWrap(True)
        main_layout.addWidget(info_label)

        global_layout = QHBoxLayout()

        self.btn_all_everything = QPushButton("Pobierz WSZYSTKO (Wszystkie tickery)")
        self.btn_all_everything.clicked.connect(
            lambda checked: self.start_bulk_download(self.tickers_map, self.ranges)
        )
        global_layout.addWidget(self.btn_all_everything)

        self.btn_all_crypto = QPushButton("Pobierz KRYPTO (Tylko cyfrowe aktywa)")
        self.btn_all_crypto.clicked.connect(
            lambda checked: self.start_bulk_download(self.crypto_tickers_map, self.ranges)
        )
        global_layout.addWidget(self.btn_all_crypto)

        main_layout.addLayout(global_layout)

        self.btn_generate_report = QPushButton(
            "📄 Generuj jednolity raport syntetyczny dla AI"
        )
        self.btn_generate_report.clicked.connect(self.generate_ai_report_file)
        main_layout.addWidget(self.btn_generate_report)

        options_group = QGroupBox("Opcje raportu")
        options_layout = QVBoxLayout()

        self.chk_only_checked = QCheckBox("Generuj raport tylko dla zaznaczonych tickerów")
        self.chk_only_checked.setChecked(True)

        self.chk_timestamp = QCheckBox("Dołącz timestamp ostatniej świecy")
        self.chk_timestamp.setChecked(True)

        self.chk_bars = QCheckBox("Dołącz liczbę świec w zakresie")
        self.chk_bars.setChecked(True)

        self.chk_minmax = QCheckBox("Dołącz minimum/maksimum zakresu")
        self.chk_minmax.setChecked(True)

        self.chk_trend = QCheckBox("Dołącz prostą ocenę trendu")
        self.chk_trend.setChecked(True)

        self.chk_instruction = QCheckBox("Dołącz instrukcję dla AI")
        self.chk_instruction.setChecked(True)

        options_layout.addWidget(self.chk_only_checked)
        options_layout.addWidget(self.chk_timestamp)
        options_layout.addWidget(self.chk_bars)
        options_layout.addWidget(self.chk_minmax)
        options_layout.addWidget(self.chk_trend)
        options_layout.addWidget(self.chk_instruction)

        options_group.setLayout(options_layout)
        main_layout.addWidget(options_group)

        selection_layout = QHBoxLayout()

        self.btn_select_all = QPushButton("Zaznacz wszystkie tickery")
        self.btn_select_all.clicked.connect(
            lambda checked, state=True: self.set_all_tickers_checked(state)
        )

        self.btn_unselect_all = QPushButton("Odznacz wszystkie tickery")
        self.btn_unselect_all.clicked.connect(
            lambda checked, state=False: self.set_all_tickers_checked(state)
        )

        selection_layout.addWidget(self.btn_select_all)
        selection_layout.addWidget(self.btn_unselect_all)
        selection_layout.addStretch()

        main_layout.addLayout(selection_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        scroll_content = QWidget()
        grid_layout = QGridLayout(scroll_content)

        headers = [
            "Uwzględnij",
            "Ticker",
            "Miesiąc (1d)",
            "Tydzień (1h)",
            "Dzień (5m)",
        ]

        for col_idx, header in enumerate(headers):
            grid_layout.addWidget(QLabel(f"<b>{header}</b>"), 0, col_idx)

        row = 1
        for label_name, yf_ticker in self.tickers_map.items():
            checkbox = QCheckBox()
            checkbox.setChecked(True)
            self.ticker_checkboxes[label_name] = checkbox

            grid_layout.addWidget(checkbox, row, 0)
            grid_layout.addWidget(QLabel(label_name), row, 1)

            for col_idx, (range_suffix, period, interval) in enumerate(self.ranges, start=2):
                btn = QPushButton(f"Pobierz {range_suffix}")
                btn.clicked.connect(
                    lambda checked,
                    t=yf_ticker,
                    l=label_name,
                    p=period,
                    i=interval,
                    s=range_suffix: self.start_download(t, l, p, i, s)
                )
                grid_layout.addWidget(btn, row, col_idx)

            row += 1

        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        self.status_label = QLabel("Gotowy do pracy.")
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)

        self.setCentralWidget(central_widget)

    def set_all_tickers_checked(self, state: bool):
        for checkbox in self.ticker_checkboxes.values():
            checkbox.setChecked(state)

    def start_download(self, yf_ticker: str, label_name: str, period: str, interval: str, suffix: str):
        file_path = make_file_path(TARGET_DIRECTORY, label_name, suffix)

        self.status_label.setText(f"Pobieranie danych dla {label_name} ({suffix})...")

        worker = DownloadWorker(yf_ticker, period, interval, file_path)
        worker.download_finished.connect(self.on_download_finished)
        self.workers.append(worker)
        worker.start()

    def start_bulk_download(self, target_map: dict, ranges_to_fetch: list):
        self.status_label.setText("Trwa masowe pobieranie danych...")

        worker = BulkDownloadWorker(target_map, ranges_to_fetch, TARGET_DIRECTORY)
        worker.progress.connect(self.status_label.setText)
        worker.bulk_finished.connect(self.on_bulk_finished)
        self.workers.append(worker)
        worker.start()

    def _cleanup_sender_worker(self):
        worker = self.sender()
        if worker is not None:
            if worker in self.workers:
                self.workers.remove(worker)
            worker.deleteLater()

    def on_download_finished(self, ticker: str, result_info: str, success: bool):
        self._cleanup_sender_worker()

        display_name = next(
            (k for k, v in self.tickers_map.items() if v == ticker),
            ticker,
        )

        if success:
            self.status_label.setText(
                f"Sukces! Zapisano plik: {os.path.basename(result_info)}"
            )
        else:
            self.status_label.setText(f"Błąd dla {display_name}: {result_info}")

    def on_bulk_finished(self, message: str, success: bool):
        self._cleanup_sender_worker()
        self.status_label.setText(message)

    @staticmethod
    def format_price(value):
        """
        Formatuje cenę tak, żeby nie gubić małych wartości,
        np. SOL-BTC albo BTC/SOL.
        """
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "?"

        abs_value = abs(value)

        if abs_value >= 1:
            return f"{value:.2f}"
        if abs_value >= 0.01:
            return f"{value:.4f}"
        return f"{value:.8f}"

    def _empty_summary(self, status: str) -> dict:
        return {
            "status": status,
            "price": None,
            "pct": None,
            "last_ts": "",
            "min": None,
            "max": None,
            "bars": 0,
        }

    def _find_column_idx(self, columns: list, wanted_names: list) -> int:
        for wanted_name in wanted_names:
            for idx, col in enumerate(columns):
                if isinstance(col, list):
                    col_name = col[0] if col else None
                else:
                    col_name = col

                if col_name == wanted_name:
                    return idx

        return -1

    def _summarize_file(self, file_path: str) -> dict:
        summary = self._empty_summary("Brak pliku")

        if not os.path.exists(file_path):
            return summary

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            columns = data.get("columns", [])
            rows = data.get("data", [])
            index = data.get("index", [])

            summary["bars"] = len(rows)

            close_idx = self._find_column_idx(columns, ["Close", "Adj Close"])
            high_idx = self._find_column_idx(columns, ["High"])
            low_idx = self._find_column_idx(columns, ["Low"])

            if close_idx == -1:
                summary["status"] = "Brak kolumny Close"
                return summary

            first_close = None
            last_close = None
            min_price = None
            max_price = None
            last_timestamp = ""

            for i, row in enumerate(rows):
                if len(row) <= close_idx:
                    continue

                close_value = row[close_idx]

                high_value = None
                low_value = None

                if high_idx != -1 and len(row) > high_idx:
                    high_value = row[high_idx]

                if low_idx != -1 and len(row) > low_idx:
                    low_value = row[low_idx]

                numeric_values = [
                    v for v in (high_value, low_value, close_value)
                    if isinstance(v, (int, float))
                ]

                for value in numeric_values:
                    if min_price is None or value < min_price:
                        min_price = value
                    if max_price is None or value > max_price:
                        max_price = value

                if isinstance(close_value, (int, float)):
                    if first_close is None:
                        first_close = close_value
                    last_close = close_value

                    if index and i < len(index):
                        last_timestamp = index[i]

            if first_close is None or last_close is None or first_close == 0:
                summary["status"] = "Brak danych"
                return summary

            change_pct = ((last_close - first_close) / first_close) * 100

            summary.update(
                {
                    "status": "OK",
                    "price": last_close,
                    "pct": change_pct,
                    "last_ts": last_timestamp or (index[-1] if index else ""),
                    "min": min_price,
                    "max": max_price,
                }
            )

            return summary

        except Exception:
            return self._empty_summary("Błąd odczytu")

    def _format_summary(self, summary: dict) -> str:
        if summary.get("status") != "OK":
            return summary.get("status", "Brak danych")

        price = summary.get("price")
        pct = summary.get("pct")

        if price is None or pct is None:
            return "Brak danych"

        parts = [f"Cena: {self.format_price(price)} ({pct:+.2f}%)"]

        if self.chk_minmax.isChecked():
            min_price = summary.get("min")
            max_price = summary.get("max")
            if min_price is not None and max_price is not None:
                parts.append(
                    f"min/max: {self.format_price(min_price)}/{self.format_price(max_price)}"
                )

        if self.chk_bars.isChecked():
            parts.append(f"świece: {summary.get('bars', 0)}")

        if self.chk_timestamp.isChecked() and summary.get("last_ts"):
            parts.append(f"ostatni zapis: {summary.get('last_ts')}")

        return " | ".join(parts)

    def _trend_comment(self, month_pct, week_pct, day_pct) -> str:
        if None in (month_pct, week_pct, day_pct):
            return "Trend: brak pełnych danych"

        if month_pct > 0 and week_pct > 0 and day_pct > 0:
            return "Trend: wzrostowy na wszystkich interwałach"

        if month_pct < 0 and week_pct < 0 and day_pct < 0:
            return "Trend: spadkowy na wszystkich interwałach"

        if month_pct > 0 and week_pct > 0 and day_pct < 0:
            return "Trend: wzrostowy średnioterminowo, krótkoterminowa korekta"

        if month_pct < 0 and week_pct < 0 and day_pct > 0:
            return "Trend: spadkowy średnioterminowo, krótkoterminowe odbicie"

        if month_pct > 0 and week_pct < 0 and day_pct > 0:
            return "Trend: miesięczny wzrostowy, tygodniowa korekta, dzienny powrót wzrostu"

        if month_pct < 0 and week_pct > 0 and day_pct < 0:
            return "Trend: miesięczny spadkowy, tygodniowe odbicie, dzienna korekta"

        return "Trend: mieszany"

    def generate_ai_report_file(self):
        if not os.path.exists(TARGET_DIRECTORY):
            self.status_label.setText("Brak folderu z danymi! Najpierw pobierz dane.")
            return

        if self.chk_only_checked.isChecked():
            selected_labels = [
                label_name
                for label_name, checkbox in self.ticker_checkboxes.items()
                if checkbox.isChecked()
            ]
            selection_info = "tylko zaznaczone tickery"
        else:
            selected_labels = list(self.tickers_map.keys())
            selection_info = "wszystkie tickery"

        if not selected_labels:
            self.status_label.setText("Nie zaznaczono żadnych tickerów do raportu.")
            return

        market_lines = []
        trend_lines = []

        for label_name in selected_labels:
            month_summary = self._summarize_file(
                make_file_path(TARGET_DIRECTORY, label_name, "miesiac")
            )
            week_summary = self._summarize_file(
                make_file_path(TARGET_DIRECTORY, label_name, "tydzien")
            )
            day_summary = self._summarize_file(
                make_file_path(TARGET_DIRECTORY, label_name, "dzien")
            )

            month_text = self._format_summary(month_summary)
            week_text = self._format_summary(week_summary)
            day_text = self._format_summary(day_summary)

            market_lines.append(
                f"- **{label_name}** | "
                f"🟢 Miesiąc (1d): {month_text} | "
                f"🟡 Tydzień (1h): {week_text} | "
                f"🔴 Dzień (5m): {day_text}"
            )

            if self.chk_trend.isChecked():
                trend = self._trend_comment(
                    month_summary.get("pct"),
                    week_summary.get("pct"),
                    day_summary.get("pct"),
                )
                trend_lines.append(f"- **{label_name}**: {trend}")

        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_lines = [
            "# RAPORT RYNKOWY DLA AI (Aktualny stan wszystkich instrumentów - Makro i Krypto)",
            "",
            f"Wygenerowano: {generated_at}",
            f"Zakres raportu: {selection_info}",
            "",
            "## Zestawienie wielookresowe (Miesiąc / Tydzień / Dzień)",
            "",
        ]

        report_lines.extend(market_lines)

        if self.chk_trend.isChecked() and trend_lines:
            report_lines.extend(
                [
                    "",
                    "## Prosta ocena trendu",
                    "",
                ]
            )
            report_lines.extend(trend_lines)

        if self.chk_instruction.isChecked():
            report_lines.extend(
                [
                    "",
                    "## Instrukcja dla modelu AI",
                    "",
                    "Przeanalizuj powyższe dane wielookresowe dla wszystkich instrumentów "
                    "(w tym ekosystemu Solany i Bitcoina) jako jedną spójną całość:",
                    "1. Oceń ogólny stan i nastroje na globalnym rynku oraz rynku kryptowalut.",
                    "2. Sprawdź spójność trendów między poszczególnymi interwałami czasowymi "
                    "(miesiąc, tydzień, dzień).",
                    "3. Wskaż najważniejsze korelacje i anomalie pomiędzy tradycyjnymi indeksami, "
                    "surowcami, walutami a aktywami cyfrowymi (Solana, BTC, ETH, MSTR, COIN).",
                    "4. Jeśli część danych ma status 'Brak pliku', 'Brak danych' lub 'Błąd odczytu', "
                    "traktuj to jako brak potwierdzenia danych, a nie jako sygnał rynkowy.",
                ]
            )

        report_content = "\n".join(report_lines) + "\n"
        report_path = os.path.join(TARGET_DIRECTORY, "raport_dla_ai.md")

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_content)
            self.status_label.setText(f"Wygenerowano jednolity raport AI: {report_path}")
        except Exception as e:
            self.status_label.setText(f"Błąd zapisu raportu: {str(e)}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet("QWidget { font-size: 12pt; }")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())