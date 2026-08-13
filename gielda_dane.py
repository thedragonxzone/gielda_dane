import json
import os
import re
import sys
from datetime import datetime

import pandas as pd
import yfinance as yf
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIRECTORY = os.path.join(BASE_DIR, "pobrane_dane")

# Progi do raportu "dla człowieka".
# Jeśli zmiana mieści się w progu, przyjmujemy konsolidację / ruch nieznaczny.
CHANGE_THRESHOLDS = {
    "miesiac": 1.5,
    "tydzien": 1.0,
    "dzien": 0.7,
}

# Orientacyjna grupa wpływająca na SOL:
# NQ1!, NVDA, ETH-USD, BTC-USD jako otoczenie technologiczno-ryzykowe.
GROUP_COMPONENTS = ["NQ1!", "NVDA", "ETH-USD", "BTC-USD"]
GROUP_TARGET = "SOL-USD"

PROFILE_NAMES = [
    "ALTSEASON_ROTATION",
    "BEAR_BOUNCE_CAUTIOUS",
    "BEAR_TREND_DEFENSIVE",
    "BTC_LEADERSHIP_ALTS_WEAK",
    "BULL_PULLBACK_CONSTRUCTIVE",
    "BULL_TREND_MOMENTUM",
    "FALLBACK_NEUTRAL",
    "HIGH_VOL_STRESS",
    "MACRO_RISK_OFF_DEFENSIVE",
    "RANGE_CHOP_SELECTIVE",
    "RANGE_COMPRESSION_WAIT",
    "SOL_RECOVERY_SELECTIVE",
]

PROFILE_PRIORITY = [
    "HIGH_VOL_STRESS",
    "MACRO_RISK_OFF_DEFENSIVE",
    "BEAR_TREND_DEFENSIVE",
    "BULL_TREND_MOMENTUM",
    "BULL_PULLBACK_CONSTRUCTIVE",
    "ALTSEASON_ROTATION",
    "SOL_RECOVERY_SELECTIVE",
    "BEAR_BOUNCE_CAUTIOUS",
    "BTC_LEADERSHIP_ALTS_WEAK",
    "RANGE_COMPRESSION_WAIT",
    "RANGE_CHOP_SELECTIVE",
    "FALLBACK_NEUTRAL",
]


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
    Wspólna funkcja do budowania ścieżki pliku JSON.
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


def classify_change(pct, suffix: str) -> str:
    """
    Klasyfikuje zmianę procentową jako:
        wzrost / spadek / konsolidacja / brak
    """
    if pct is None:
        return "brak"

    threshold = CHANGE_THRESHOLDS.get(suffix, 1.0)

    if pct > threshold:
        return "wzrost"

    if pct < -threshold:
        return "spadek"

    return "konsolidacja"


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

        self.setWindowTitle("Panel rynku — dane, grupa SOL i raporty")
        self.resize(1350, 900)

        self.bulk_worker = None

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
            # Krypto i powiązane:
            "SOL-USD": "SOL-USD",
            "SOL-BTC": "SOL-BTC",
            "BTC-USD": "BTC-USD",
            "ETH-USD": "ETH-USD",
            "COIN": "COIN",
            "MSTR": "MSTR",
            # Dodatkowe dane makro / altseason:
            "VIX": "^VIX",
            "US10Y": "^TNX",
            "ETH-BTC": "ETH-BTC",
        }

        self.crypto_tickers_map = {
            "SOL-USD": "SOL-USD",
            "SOL-BTC": "SOL-BTC",
            "BTC-USD": "BTC-USD",
            "ETH-USD": "ETH-USD",
            "COIN": "COIN",
            "MSTR": "MSTR",
            "ETH-BTC": "ETH-BTC",
        }

        self.context_map = {
            "NQ1!": "Grupa SOL / Nasdaq futures",
            "ni225": "Indeks Japonii",
            "USD/JPY": "FX / risk sentiment",
            "DXY": "Dolar / makro",
            "HSTECH": "Indeks tech HK",
            "HSI": "Indeks HK",
            "CL1!": "Ropa",
            "GC1!": "Złoto",
            "000001.SS": "Indeks Szanghaj",
            "ES1!": "S&P futures",
            "DJI": "Indeks Dow Jones",
            "SOXX/SMH": "Półprzewodniki",
            "SNDK": "Spółka pamięci",
            "NVDA": "Grupa SOL / półprzewodniki",
            "SOL-USD": "Cel grupy SOL",
            "SOL-BTC": "Siła SOL względem BTC",
            "BTC-USD": "Grupa SOL / benchmark krypto",
            "ETH-USD": "Grupa SOL / benchmark alt",
            "COIN": "Ekosystem krypto",
            "MSTR": "Ekosystem krypto",
            "VIX": "Zmienność / risk-off",
            "US10Y": "Rentowności US",
            "ETH-BTC": "ETH względem BTC",
        }

        # Celowo dzień jest pobierany jako 1d, żeby raportować ostatni dzień,
        # a nie dwa dni jak w poprzedniej wersji.
        self.ranges = [
            ("miesiac", "1mo", "1d"),
            ("tydzien", "7d", "1h"),
            ("dzien", "1d", "5m"),
        ]

        self.ticker_checkboxes = {}

        self._init_ui()

    def _init_ui(self):
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        header_label = QLabel(
            f"<h3 style='margin:0;'>Panel danych rynkowych</h3>"
            f"Katalog zapisu plików: <b>{os.path.abspath(TARGET_DIRECTORY)}</b><br>"
            "Grupa referencyjna SOL liczona orientacyjnie jako średnia procentowa zmian: "
            "<b>NQ1!, NVDA, ETH-USD, BTC-USD</b> i porównywana z <b>SOL-USD</b>."
        )
        header_label.setWordWrap(True)
        main_layout.addWidget(header_label)

        # --------------------------------------------------
        # 1. Pobieranie danych
        # --------------------------------------------------
        download_group = QGroupBox("1. Pobieranie danych")
        download_layout = QVBoxLayout()

        download_buttons_layout = QHBoxLayout()

        self.btn_all_everything = QPushButton("⬇ Pobierz WSZYSTKO (wszystkie tickery)")
        self.btn_all_everything.clicked.connect(
            lambda checked: self.start_bulk_download(self.tickers_map)
        )

        self.btn_all_crypto = QPushButton("⬇ Pobierz KRYPTO (tylko cyfrowe aktywa)")
        self.btn_all_crypto.clicked.connect(
            lambda checked: self.start_bulk_download(self.crypto_tickers_map)
        )

        download_buttons_layout.addWidget(self.btn_all_everything)
        download_buttons_layout.addWidget(self.btn_all_crypto)
        download_buttons_layout.addStretch()

        download_note = QLabel(
            "Każdy z przycisków pobiera komplet zakresów: miesiąc / tydzień / dzień."
        )
        download_note.setStyleSheet("color:#555;")

        download_layout.addLayout(download_buttons_layout)
        download_layout.addWidget(download_note)
        download_group.setLayout(download_layout)
        main_layout.addWidget(download_group)

        # --------------------------------------------------
        # 2. Analiza
        # --------------------------------------------------
        analysis_group = QGroupBox("2. Analiza")
        analysis_layout = QVBoxLayout()

        analysis_buttons_layout = QHBoxLayout()

        self.btn_group_image = QPushButton("🧩 Pokaż obraz grupy SOL (NQ1! / NVDA / ETH / BTC)")
        self.btn_group_image.clicked.connect(self.show_group_image)

        self.btn_suggest_profile = QPushButton("🧠 Podpowiedz profil SOL/BTC")
        self.btn_suggest_profile.clicked.connect(self.show_profile_suggestion)

        analysis_buttons_layout.addWidget(self.btn_group_image)
        analysis_buttons_layout.addWidget(self.btn_suggest_profile)
        analysis_buttons_layout.addStretch()

        analysis_note = QLabel(
            "Obraz grupy jest orientacyjny. To nie jest jeden twardy wskaźnik techniczny, "
            "tylko synteza zachowania NQ1!, NVDA, ETH i BTC jako otoczenia dla SOL."
        )
        analysis_note.setWordWrap(True)
        analysis_note.setStyleSheet("color:#555;")

        analysis_layout.addLayout(analysis_buttons_layout)
        analysis_layout.addWidget(analysis_note)
        analysis_group.setLayout(analysis_layout)
        main_layout.addWidget(analysis_group)

        # --------------------------------------------------
        # 3. Raporty
        # --------------------------------------------------
        reports_group = QGroupBox("3. Raporty")
        reports_layout = QVBoxLayout()

        report_buttons_layout = QHBoxLayout()

        self.btn_report_json = QPushButton("📊 Raport analityczny (JSON)")
        self.btn_report_json.clicked.connect(self.generate_analytical_report_file)

        self.btn_report_ai = QPushButton("🤖 Raport dla AI (MD)")
        self.btn_report_ai.clicked.connect(self.generate_ai_report_file)

        self.btn_report_human = QPushButton("👤 Raport dla człowieka (MD)")
        self.btn_report_human.clicked.connect(self.generate_human_report_file)

        report_buttons_layout.addWidget(self.btn_report_json)
        report_buttons_layout.addWidget(self.btn_report_ai)
        report_buttons_layout.addWidget(self.btn_report_human)
        report_buttons_layout.addStretch()

        reports_note = QLabel(
            "Raport analityczny jest maszynowy (JSON), raport AI opisowy (Markdown), "
            "a raport dla człowieka tabelaryczny z kolorowymi oznaczeniami."
        )
        reports_note.setWordWrap(True)
        reports_note.setStyleSheet("color:#555;")

        reports_layout.addLayout(report_buttons_layout)
        reports_layout.addWidget(reports_note)
        reports_group.setLayout(reports_layout)
        main_layout.addWidget(reports_group)

        # --------------------------------------------------
        # 4. Opcje raportów
        # --------------------------------------------------
        options_group = QGroupBox("4. Opcje raportów")
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

        self.chk_profile = QCheckBox("Dołącz sugestię profilu SOL/BTC do raportu")
        self.chk_profile.setChecked(True)

        self.chk_group = QCheckBox("Dołącz grupę referencyjną SOL do raportu")
        self.chk_group.setChecked(True)

        options_layout.addWidget(self.chk_only_checked)
        options_layout.addWidget(self.chk_timestamp)
        options_layout.addWidget(self.chk_bars)
        options_layout.addWidget(self.chk_minmax)
        options_layout.addWidget(self.chk_trend)
        options_layout.addWidget(self.chk_instruction)
        options_layout.addWidget(self.chk_profile)
        options_layout.addWidget(self.chk_group)

        options_group.setLayout(options_layout)
        main_layout.addWidget(options_group)

        # --------------------------------------------------
        # 5. Tickery
        # --------------------------------------------------
        tickers_group = QGroupBox("5. Tickery")
        tickers_layout = QVBoxLayout()

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

        tickers_layout.addLayout(selection_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        scroll_content = QWidget()
        grid_layout = QGridLayout(scroll_content)
        grid_layout.setVerticalSpacing(6)

        headers = [
            "Uwzględnij",
            "Ticker",
            "Symbol Yahoo Finance",
            "Kontekst",
        ]

        for col_idx, header in enumerate(headers):
            grid_layout.addWidget(QLabel(f"<b>{header}</b>"), 0, col_idx)

        row = 1

        for label_name, yf_ticker in self.tickers_map.items():
            checkbox = QCheckBox()
            checkbox.setChecked(True)
            self.ticker_checkboxes[label_name] = checkbox

            grid_layout.addWidget(checkbox, row, 0)
            grid_layout.addWidget(QLabel(f"<b>{label_name}</b>"), row, 1)
            grid_layout.addWidget(QLabel(yf_ticker), row, 2)
            grid_layout.addWidget(QLabel(self.context_map.get(label_name, "")), row, 3)

            row += 1

        grid_layout.setColumnStretch(1, 2)
        grid_layout.setColumnStretch(3, 3)

        scroll.setWidget(scroll_content)
        tickers_layout.addWidget(scroll)
        tickers_group.setLayout(tickers_layout)
        main_layout.addWidget(tickers_group)

        # --------------------------------------------------
        # Status
        # --------------------------------------------------
        self.status_label = QLabel("Gotowy do pracy.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#234; font-weight:bold;")
        main_layout.addWidget(self.status_label)

        main_layout.addStretch()

        self.setCentralWidget(central_widget)

    # ------------------------------------------------------
    # Podstawowe akcje GUI
    # ------------------------------------------------------

    def set_all_tickers_checked(self, state: bool):
        for checkbox in self.ticker_checkboxes.values():
            checkbox.setChecked(state)

    def start_bulk_download(self, target_map: dict):
        if self.bulk_worker is not None and self.bulk_worker.isRunning():
            self.status_label.setText("Pobieranie już trwa. Poczekaj na zakończenie.")
            return

        self.status_label.setText("Trwa masowe pobieranie danych...")

        worker = BulkDownloadWorker(target_map, self.ranges, TARGET_DIRECTORY)
        worker.progress.connect(self.status_label.setText)
        worker.bulk_finished.connect(self.on_bulk_finished)

        self.bulk_worker = worker
        worker.start()

    def _cleanup_sender_worker(self):
        worker = self.sender()
        if worker is None:
            return

        if worker is self.bulk_worker:
            self.bulk_worker = None

        worker.deleteLater()

    def on_bulk_finished(self, message: str, success: bool):
        self._cleanup_sender_worker()
        self.status_label.setText(message)

    # ------------------------------------------------------
    # Pomocnicze formatowanie
    # ------------------------------------------------------

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

    @staticmethod
    def _fmt_pct(value):
        if isinstance(value, (int, float)):
            return f"{value:+.2f}%"
        return "brak"

    @staticmethod
    def _class_to_signal(class_name: str) -> float:
        return {
            "wzrost": 1.0,
            "spadek": -1.0,
            "konsolidacja": 0.0,
        }.get(class_name, 0.0)

    def _dot_text(self, pct, suffix: str) -> str:
        """
        Zwraca tekst z kropką/kolorem i procentem do raportu dla człowieka.
        """
        if pct is None:
            return "⚪ brak"

        class_name = classify_change(pct, suffix)

        dot = {
            "wzrost": "🟢",
            "spadek": "🔴",
            "konsolidacja": "🔵",
        }.get(class_name, "⚪")

        return f"{dot} {pct:+.2f}%"

    def _human_cell(self, summary: dict, suffix: str) -> str:
        if not self._is_ok(summary):
            return "⚪ brak"

        return self._dot_text(summary.get("pct"), suffix)

    # ------------------------------------------------------
    # Odczyt i streszczanie plików
    # ------------------------------------------------------

    def _empty_summary(self, status: str) -> dict:
        return {
            "status": status,
            "price": None,
            "pct": None,
            "last_ts": "",
            "min": None,
            "max": None,
            "bars": 0,
            "range_pct": None,
            "range_pos": None,
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

            if min_price is not None and max_price is not None and last_close is not None:
                denom = abs(float(last_close))

                if denom > 0:
                    summary["range_pct"] = ((max_price - min_price) / denom) * 100.0

                    if max_price > min_price:
                        summary["range_pos"] = (last_close - min_price) / (max_price - min_price)
                    else:
                        summary["range_pos"] = 0.5

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

    # ------------------------------------------------------
    # Wybór tickerów do raportów
    # ------------------------------------------------------

    def _selected_labels(self):
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

        return selected_labels, selection_info

    # ------------------------------------------------------
    # Pobieranie / streszczanie dla konkretnego label/suffix
    # ------------------------------------------------------

    def _get_summary(self, label_name: str, suffix: str) -> dict:
        return self._summarize_file(make_file_path(TARGET_DIRECTORY, label_name, suffix))

    def _tf(self, label_name: str) -> dict:
        return {
            "month": self._get_summary(label_name, "miesiac"),
            "week": self._get_summary(label_name, "tydzien"),
            "day": self._get_summary(label_name, "dzien"),
        }

    @staticmethod
    def _is_ok(summary: dict) -> bool:
        return isinstance(summary, dict) and summary.get("status") == "OK"

    @staticmethod
    def _pct(summary: dict):
        if isinstance(summary, dict) and summary.get("status") == "OK":
            return summary.get("pct")
        return None

    @staticmethod
    def _price(summary: dict):
        if isinstance(summary, dict) and summary.get("status") == "OK":
            return summary.get("price")
        return None

    def _trend_score(self, tf: dict) -> float:
        """
        Prosty ważony score trendu:
        miesiąc = 1, tydzień = 2, dzień = 3.
        """
        score = 0.0
        weights = {"month": 1.0, "week": 2.0, "day": 3.0}

        for key, weight in weights.items():
            pct = self._pct(tf.get(key, {}))

            if pct is None:
                continue

            if pct > 0.05:
                score += weight
            elif pct < -0.05:
                score -= weight

        return score

    def _avg_trend_score(self, labels: list):
        scores = []

        for label in labels:
            tf = self._tf(label)

            if any(self._is_ok(tf[key]) for key in ("month", "week", "day")):
                scores.append(self._trend_score(tf))

        return sum(scores) / len(scores) if scores else None

    def _range_pct(self, label_name: str, suffix: str):
        summary = self._get_summary(label_name, suffix)

        if self._is_ok(summary):
            return summary.get("range_pct")

        return None

    # ------------------------------------------------------
    # Grupa referencyjna SOL
    # ------------------------------------------------------

    def _compute_group_image(self) -> dict:
        """
        Orientacyjna grupa wpływająca na SOL:
        NQ1! / NVDA / ETH / BTC.

        Liczenie jest proste i celowo orientacyjne:
        - dla każdego interwału bierzemy zmiany procentowe komponentów,
        - liczymy z nich średnią,
        - porównujemy średnią grupy z wynikiem SOL,
        - klasyfikujemy: wzrost / spadek / konsolidacja,
        - dodajemy relację SOL vs grupa.
        """
        intervals = [
            ("miesiac", "Miesiąc (1d)"),
            ("tydzien", "Tydzień (1h)"),
            ("dzien", "Dzień (5m)"),
        ]

        weights = {
            "miesiac": 1.0,
            "tydzien": 2.0,
            "dzien": 3.0,
        }

        result = {
            "name": "Grupa referencyjna SOL",
            "components": GROUP_COMPONENTS,
            "target": GROUP_TARGET,
            "method": "Średnia procentowa zmian komponentów w danym interwale vs SOL",
            "intervals": {},
        }

        group_weighted = 0.0
        group_weight_sum = 0.0
        sol_weighted = 0.0
        sol_weight_sum = 0.0

        for suffix, interval_label in intervals:
            component_data = {}
            pcts = []

            for comp in GROUP_COMPONENTS:
                summary = self._get_summary(comp, suffix)
                pct = self._pct(summary)

                component_data[comp] = {
                    "pct": pct,
                    "price": self._price(summary),
                    "status": summary.get("status", "Brak danych"),
                }

                if pct is not None:
                    pcts.append(pct)

            avg_pct = sum(pcts) / len(pcts) if pcts else None

            sol_summary = self._get_summary(GROUP_TARGET, suffix)
            sol_pct = self._pct(sol_summary)

            group_class = classify_change(avg_pct, suffix)

            relation = "brak"

            if avg_pct is not None and sol_pct is not None:
                threshold = CHANGE_THRESHOLDS.get(suffix, 1.0)
                diff = sol_pct - avg_pct

                if diff > threshold / 2:
                    relation = "SOL mocniejszy niż grupa"
                elif diff < -threshold / 2:
                    relation = "SOL słabszy niż grupa"
                else:
                    relation = "SOL porusza się zgodnie z grupą"

            result["intervals"][suffix] = {
                "label": interval_label,
                "component_pcts": component_data,
                "avg_pct": avg_pct,
                "sol_pct": sol_pct,
                "signal": group_class,
                "relation": relation,
            }

            if avg_pct is not None:
                group_weighted += weights[suffix] * self._class_to_signal(group_class)
                group_weight_sum += weights[suffix]

            if sol_pct is not None:
                sol_class = classify_change(sol_pct, suffix)
                sol_weighted += weights[suffix] * self._class_to_signal(sol_class)
                sol_weight_sum += weights[suffix]

        group_score = group_weighted / group_weight_sum if group_weight_sum else 0.0
        sol_score = sol_weighted / sol_weight_sum if sol_weight_sum else 0.0

        if group_score > 0.25:
            if sol_score < -0.25:
                overall_signal = "Otoczenie grupy jest wzrostowe, ale SOL tego nie potwierdza"
            elif sol_score < group_score - 0.25:
                overall_signal = "Otoczenie grupy wspiera SOL, ale SOL pozostaje względnie słabszy"
            else:
                overall_signal = "Otoczenie grupy wspiera SOL"
        elif group_score < -0.25:
            if sol_score > 0.25:
                overall_signal = "Otoczenie grupy jest spadkowe, ale SOL wykazuje względną siłę"
            else:
                overall_signal = "Otoczenie grupy ciąży SOL"
        else:
            overall_signal = "Otoczenie grupy jest mieszane / neutralne dla SOL"

        result.update(
            {
                "group_score": group_score,
                "sol_score": sol_score,
                "overall_signal": overall_signal,
            }
        )

        result["lines"] = self._format_group_lines(result)

        return result

    def _format_group_lines(self, group: dict) -> list:
        lines = [
            "Grupa referencyjna SOL: " + ", ".join(group.get("components", [])),
            f"Odbiorca sygnału: {group.get('target', GROUP_TARGET)}",
            f"Metoda: {group.get('method', 'brak')}",
            f"Interpretacja ogólna: {group.get('overall_signal', 'brak')}",
            "",
        ]

        for suffix, interval_label in [
            ("miesiac", "Miesiąc (1d)"),
            ("tydzien", "Tydzień (1h)"),
            ("dzien", "Dzień (5m)"),
        ]:
            data = group.get("intervals", {}).get(suffix, {})

            avg_pct = data.get("avg_pct")
            sol_pct = data.get("sol_pct")
            signal = data.get("signal", "brak")
            relation = data.get("relation", "brak")

            lines.append(
                f"- {interval_label}: średnia grupy {self._fmt_pct(avg_pct)}, "
                f"SOL {self._fmt_pct(sol_pct)}; sygnał: {signal}; relacja: {relation}"
            )

        return lines

    def show_group_image(self):
        try:
            result = self._compute_group_image()
        except Exception as e:
            QMessageBox.critical(
                self,
                "Błąd",
                f"Nie udało się obliczyć obrazu grupy:\n\n{str(e)}",
            )
            return

        text = "\n".join(result["lines"])

        try:
            os.makedirs(TARGET_DIRECTORY, exist_ok=True)
            group_path = os.path.join(TARGET_DIRECTORY, "obraz_grupy_sol.md")

            with open(group_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")

            self.status_label.setText(f"Zapisano obraz grupy SOL: {group_path}")

        except Exception:
            self.status_label.setText(
                "Obraz grupy SOL obliczony, ale nie udało się zapisać pliku obraz_grupy_sol.md."
            )

        QMessageBox.information(self, "Obraz grupy SOL", text)

    # ------------------------------------------------------
    # Raport analityczny JSON
    # ------------------------------------------------------

    def generate_analytical_report_file(self):
        selected_labels, selection_info = self._selected_labels()

        if not selected_labels:
            self.status_label.setText("Nie zaznaczono żadnych tickerów do raportu.")
            return

        group_image = None
        if self.chk_group.isChecked():
            try:
                group_image = self._compute_group_image()
            except Exception as e:
                group_image = {"error": str(e)}

        profile_suggestion = None
        if self.chk_profile.isChecked():
            try:
                profile_suggestion = self._compute_profile_suggestion()
            except Exception as e:
                profile_suggestion = {"error": str(e)}

        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "selection_info": selection_info,
            "options": {
                "only_checked": self.chk_only_checked.isChecked(),
                "timestamp": self.chk_timestamp.isChecked(),
                "bars": self.chk_bars.isChecked(),
                "minmax": self.chk_minmax.isChecked(),
                "trend": self.chk_trend.isChecked(),
                "instruction": self.chk_instruction.isChecked(),
                "profile": self.chk_profile.isChecked(),
                "group": self.chk_group.isChecked(),
            },
            "ranges": [
                {
                    "suffix": suffix,
                    "period": period,
                    "interval": interval,
                }
                for suffix, period, interval in self.ranges
            ],
            "change_thresholds": CHANGE_THRESHOLDS,
            "group_image": group_image,
            "profile_suggestion": profile_suggestion,
            "tickers": {},
            "trend_comments": {},
        }

        for label_name in selected_labels:
            month_summary = self._get_summary(label_name, "miesiac")
            week_summary = self._get_summary(label_name, "tydzien")
            day_summary = self._get_summary(label_name, "dzien")

            payload["tickers"][label_name] = {
                "miesiac": month_summary,
                "tydzien": week_summary,
                "dzien": day_summary,
            }

            if self.chk_trend.isChecked():
                payload["trend_comments"][label_name] = self._trend_comment(
                    month_summary.get("pct"),
                    week_summary.get("pct"),
                    day_summary.get("pct"),
                )

        report_path = os.path.join(TARGET_DIRECTORY, "raport_analityczny.json")

        try:
            os.makedirs(TARGET_DIRECTORY, exist_ok=True)

            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)

            self.status_label.setText(f"Wygenerowano raport analityczny: {report_path}")

        except Exception as e:
            self.status_label.setText(f"Błąd zapisu raportu analitycznego: {str(e)}")

    # ------------------------------------------------------
    # Raport dla AI
    # ------------------------------------------------------

    def generate_ai_report_file(self):
        if not os.path.exists(TARGET_DIRECTORY):
            self.status_label.setText("Brak folderu z danymi! Najpierw pobierz dane.")
            return

        selected_labels, selection_info = self._selected_labels()

        if not selected_labels:
            self.status_label.setText("Nie zaznaczono żadnych tickerów do raportu.")
            return

        market_lines = []
        trend_lines = []

        for label_name in selected_labels:
            month_summary = self._get_summary(label_name, "miesiac")
            week_summary = self._get_summary(label_name, "tydzien")
            day_summary = self._get_summary(label_name, "dzien")

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
            "# RAPORT RYNKOWY DLA AI (Aktualny stan wszystkich instrumentów — Makro i Krypto)",
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

        if self.chk_group.isChecked():
            try:
                group_image = self._compute_group_image()

                report_lines.extend(
                    [
                        "",
                        "## Grupa referencyjna SOL (NQ1! / NVDA / ETH / BTC)",
                        "",
                    ]
                )
                report_lines.extend(group_image["lines"])

            except Exception as e:
                report_lines.extend(
                    [
                        "",
                        "## Grupa referencyjna SOL (NQ1! / NVDA / ETH / BTC)",
                        "",
                        f"Błąd obliczenia grupy: {str(e)}",
                    ]
                )

        if self.chk_profile.isChecked():
            try:
                profile_result = self._compute_profile_suggestion()

                report_lines.extend(
                    [
                        "",
                        "## Sugestia profilu SOL/BTC",
                        "",
                    ]
                )
                report_lines.extend(profile_result["lines"])

            except Exception as e:
                report_lines.extend(
                    [
                        "",
                        "## Sugestia profilu SOL/BTC",
                        "",
                        f"Błąd obliczenia profilu: {str(e)}",
                    ]
                )

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
                    "4. Uwzględnij grupę referencyjną NQ1! / NVDA / ETH / BTC jako orientacyjne "
                    "otoczenie wpływające na SOL.",
                    "5. Jeśli część danych ma status 'Brak pliku', 'Brak danych' lub 'Błąd odczytu', "
                    "traktuj to jako brak potwierdzenia danych, a nie jako sygnał rynkowy.",
                ]
            )

        report_content = "\n".join(report_lines) + "\n"
        report_path = os.path.join(TARGET_DIRECTORY, "raport_dla_ai.md")

        try:
            os.makedirs(TARGET_DIRECTORY, exist_ok=True)

            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_content)

            self.status_label.setText(f"Wygenerowano raport dla AI: {report_path}")

        except Exception as e:
            self.status_label.setText(f"Błąd zapisu raportu AI: {str(e)}")

    # ------------------------------------------------------
    # Raport dla człowieka
    # ------------------------------------------------------

    def generate_human_report_file(self):
        if not os.path.exists(TARGET_DIRECTORY):
            self.status_label.setText("Brak folderu z danymi! Najpierw pobierz dane.")
            return

        selected_labels, selection_info = self._selected_labels()

        if not selected_labels:
            self.status_label.setText("Nie zaznaczono żadnych tickerów do raportu.")
            return

        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "# 📈 RAPORT RYNKOWY DLA CZŁOWIEKA",
            "",
            f"Wygenerowano: {generated_at}",
            f"Zakres raportu: {selection_info}",
            "",
            "Legenda:",
            "- 🟢 wzrost",
            "- 🔵 konsolidacja / ruch nieznaczny",
            "- 🔴 spadek",
            "- ⚪ brak danych",
            "",
            f"Progi konsolidacji: miesiąc ±{CHANGE_THRESHOLDS['miesiac']:.2f}%, "
            f"tydzień ±{CHANGE_THRESHOLDS['tydzien']:.2f}%, "
            f"dzień ±{CHANGE_THRESHOLDS['dzien']:.2f}%.",
            "",
        ]

        # --------------------------------------------------
        # Tabela główna: zmiany procentowe
        # --------------------------------------------------
        lines.extend(
            [
                "## 1. Zmiany procentowe",
                "",
                "| Instrument | Miesiąc (1d) | Tydzień (1h) | Dzień (5m) | Trend |",
                "|---|---:|---:|---:|---|",
            ]
        )

        for label_name in selected_labels:
            month_summary = self._get_summary(label_name, "miesiac")
            week_summary = self._get_summary(label_name, "tydzien")
            day_summary = self._get_summary(label_name, "dzien")

            month_cell = self._human_cell(month_summary, "miesiac")
            week_cell = self._human_cell(week_summary, "tydzien")
            day_cell = self._human_cell(day_summary, "dzien")

            if self.chk_trend.isChecked():
                trend = self._trend_comment(
                    month_summary.get("pct"),
                    week_summary.get("pct"),
                    day_summary.get("pct"),
                )
            else:
                trend = ""

            lines.append(
                f"| **{label_name}** | {month_cell} | {week_cell} | {day_cell} | {trend} |"
            )

        # --------------------------------------------------
        # Tabela szczegółów
        # --------------------------------------------------
        lines.extend(
            [
                "",
                "## 2. Szczegóły",
                "",
            ]
        )

        headers = ["Instrument", "Ostatnia cena"]

        if self.chk_minmax.isChecked():
            headers.append("Min/Max (pierwszy dostępny zakres)")

        if self.chk_bars.isChecked():
            headers.append("Świece M/T/D")

        if self.chk_timestamp.isChecked():
            headers.append("Ostatnia świeca")

        lines.append("| " + " | ".join(headers) + " |")

        separator = ["---"] + ["---:"] * (len(headers) - 1)
        lines.append("| " + " | ".join(separator) + " |")

        for label_name in selected_labels:
            month_summary = self._get_summary(label_name, "miesiac")
            week_summary = self._get_summary(label_name, "tydzien")
            day_summary = self._get_summary(label_name, "dzien")

            summaries = [day_summary, week_summary, month_summary]

            price = None
            for summary in summaries:
                if self._is_ok(summary) and summary.get("price") is not None:
                    price = summary.get("price")
                    break

            source_summary = next(
                (s for s in (month_summary, week_summary, day_summary) if self._is_ok(s)),
                {},
            )

            min_price = source_summary.get("min")
            max_price = source_summary.get("max")

            last_ts = ""
            for summary in summaries:
                if self._is_ok(summary) and summary.get("last_ts"):
                    last_ts = summary.get("last_ts")
                    break

            row = [
                f"**{label_name}**",
                self.format_price(price) if price is not None else "brak",
            ]

            if self.chk_minmax.isChecked():
                if min_price is not None and max_price is not None:
                    row.append(
                        f"{self.format_price(min_price)} / {self.format_price(max_price)}"
                    )
                else:
                    row.append("brak")

            if self.chk_bars.isChecked():
                row.append(
                    f"{month_summary.get('bars', 0)} / "
                    f"{week_summary.get('bars', 0)} / "
                    f"{day_summary.get('bars', 0)}"
                )

            if self.chk_timestamp.isChecked():
                row.append(last_ts or "brak")

            lines.append("| " + " | ".join(row) + " |")

        # --------------------------------------------------
        # Grupa referencyjna SOL
        # --------------------------------------------------
        if self.chk_group.isChecked():
            try:
                group = self._compute_group_image()

                lines.extend(
                    [
                        "",
                        "## 3. Grupa referencyjna SOL",
                        "",
                        f"**Interpretacja ogólna:** {group.get('overall_signal', 'brak')}",
                        "",
                        "Orientacyjna grupa: " + ", ".join(GROUP_COMPONENTS),
                        "Porównanie z: " + GROUP_TARGET,
                        "",
                        "| Interwał | Średnia grupy | SOL | Sygnał grupy | Relacja SOL vs grupa |",
                        "|---|---:|---:|---|---|",
                    ]
                )

                for suffix, interval_label in [
                    ("miesiac", "Miesiąc (1d)"),
                    ("tydzien", "Tydzień (1h)"),
                    ("dzien", "Dzień (5m)"),
                ]:
                    data = group.get("intervals", {}).get(suffix, {})

                    avg_pct = data.get("avg_pct")
                    sol_pct = data.get("sol_pct")
                    signal = data.get("signal", "brak")
                    relation = data.get("relation", "brak")

                    lines.append(
                        f"| {interval_label} | {self._fmt_pct(avg_pct)} | "
                        f"{self._fmt_pct(sol_pct)} | {signal} | {relation} |"
                    )

                lines.extend(
                    [
                        "",
                        "### Komponenty grupy",
                        "",
                        "| Komponent | Miesiąc (1d) | Tydzień (1h) | Dzień (5m) |",
                        "|---|---:|---:|---:|",
                    ]
                )

                for comp in GROUP_COMPONENTS:
                    cells = []

                    for suffix in ["miesiac", "tydzien", "dzien"]:
                        comp_data = group.get("intervals", {}).get(suffix, {})
                        pct = comp_data.get("component_pcts", {}).get(comp, {}).get("pct")
                        cells.append(self._dot_text(pct, suffix))

                    lines.append(f"| {comp} | " + " | ".join(cells) + " |")

            except Exception as e:
                lines.extend(
                    [
                        "",
                        "## 3. Grupa referencyjna SOL",
                        "",
                        f"Błąd obliczenia grupy: {str(e)}",
                    ]
                )

        # --------------------------------------------------
        # Profil SOL/BTC
        # --------------------------------------------------
        if self.chk_profile.isChecked():
            try:
                profile = self._compute_profile_suggestion()

                lines.extend(
                    [
                        "",
                        "## 4. Profil SOL/BTC",
                        "",
                        "```text",
                    ]
                )
                lines.extend(profile["lines"])
                lines.append("```")

            except Exception as e:
                lines.extend(
                    [
                        "",
                        "## 4. Profil SOL/BTC",
                        "",
                        f"Błąd obliczenia profilu: {str(e)}",
                    ]
                )

        lines.extend(
            [
                "",
                "---",
                "Raport ma charakter poglądowy. Oznaczenia kolorami są oparte o progi procentowe "
                "i nie stanowią analizy inwestycyjnej.",
                "",
            ]
        )

        report_content = "\n".join(lines) + "\n"
        report_path = os.path.join(TARGET_DIRECTORY, "raport_dla_czlowieka.md")

        try:
            os.makedirs(TARGET_DIRECTORY, exist_ok=True)

            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_content)

            self.status_label.setText(f"Wygenerowano raport dla człowieka: {report_path}")

        except Exception as e:
            self.status_label.setText(f"Błąd zapisu raportu dla człowieka: {str(e)}")

    # ------------------------------------------------------
    # Rekomendacja profilu SOL/BTC
    # ------------------------------------------------------

    def _compute_profile_suggestion(self) -> dict:
        core_labels = ["BTC-USD", "SOL-USD", "SOL-BTC"]
        missing = []

        for label in core_labels:
            tf = self._tf(label)

            if not any(self._is_ok(tf[key]) for key in ("month", "week", "day")):
                missing.append(label)

        if missing:
            lines = [
                "Sugerowany profil: FALLBACK_NEUTRAL",
                "",
                "Powód:",
                f"- Brak wystarczających danych: {', '.join(missing)}",
            ]

            return {
                "profile": "FALLBACK_NEUTRAL",
                "score": 0.0,
                "reasons": missing,
                "lines": lines,
            }

        btc = self._tf("BTC-USD")
        sol = self._tf("SOL-USD")
        solbtc = self._tf("SOL-BTC")
        eth = self._tf("ETH-USD")
        ethbtc = self._tf("ETH-BTC")
        dxy = self._tf("DXY")
        vix = self._tf("VIX")
        us10y = self._tf("US10Y")

        btc_score = self._trend_score(btc)
        sol_score = self._trend_score(sol)
        solbtc_score = self._trend_score(solbtc)
        eq_score = self._avg_trend_score(["NQ1!", "ES1!", "DJI"])

        btc_m = self._pct(btc.get("month", {}))
        btc_w = self._pct(btc.get("week", {}))
        btc_d = self._pct(btc.get("day", {}))

        sol_m = self._pct(sol.get("month", {}))
        sol_w = self._pct(sol.get("week", {}))
        sol_d = self._pct(sol.get("day", {}))

        solbtc_m = self._pct(solbtc.get("month", {}))
        solbtc_w = self._pct(solbtc.get("week", {}))
        solbtc_d = self._pct(solbtc.get("day", {}))

        vix_price = self._price(vix.get("day", {}))
        vix_week = self._pct(vix.get("week", {}))
        us10y_week = self._pct(us10y.get("week", {}))

        btc_day_range = self._range_pct("BTC-USD", "dzien")
        sol_day_range = self._range_pct("SOL-USD", "dzien")

        def fmt_pct(value):
            return f"{value:+.2f}%" if isinstance(value, (int, float)) else "brak"

        def fmt_num(value):
            return f"{value:.2f}" if isinstance(value, (int, float)) else "brak"

        def abs_val(value):
            return abs(value) if isinstance(value, (int, float)) else 999.0

        scores = {name: 0.0 for name in PROFILE_NAMES}
        reasons = {name: [] for name in PROFILE_NAMES}

        scores["FALLBACK_NEUTRAL"] = 0.5
        reasons["FALLBACK_NEUTRAL"].append("Profil domyślny, jeśli żaden inny nie ma przewagi.")

        def add(profile: str, points: float, reason: str = ""):
            scores[profile] += points
            if reason:
                reasons[profile].append(reason)

        # ------------------------
        # HIGH_VOL_STRESS
        # ------------------------
        if (btc_day_range or 0.0) >= 5.0 or (sol_day_range or 0.0) >= 7.0:
            add(
                "HIGH_VOL_STRESS",
                6.0,
                f"Duża dzienna zmienność BTC/SOL: {fmt_num(btc_day_range)}% / {fmt_num(sol_day_range)}%.",
            )

        if vix_price is not None and vix_price >= 24.0:
            add(
                "HIGH_VOL_STRESS",
                4.0,
                f"VIX = {vix_price:.2f} wskazuje podwyższony stres rynkowy.",
            )

        if vix_week is not None and vix_week >= 10.0:
            add(
                "HIGH_VOL_STRESS",
                3.0,
                f"VIX mocno rośnie w ujęciu tygodniowym: {fmt_pct(vix_week)}.",
            )

        # ------------------------
        # MACRO_RISK_OFF_DEFENSIVE
        # ------------------------
        dxy_week = self._pct(dxy.get("week", {})) or 0.0
        dxy_day = self._pct(dxy.get("day", {})) or 0.0

        if eq_score is not None and eq_score <= -3.0 and (
            dxy_week > 0 or dxy_day > 0 or (vix_price or 0.0) >= 20.0
        ):
            add(
                "MACRO_RISK_OFF_DEFENSIVE",
                7.0,
                "Equities są słabe, a DXY/VIX sugerują defensywne otoczenie makro.",
            )
        elif eq_score is not None and eq_score <= -1.0 and dxy_week > 0 and (vix_price or 0.0) >= 18.0:
            add(
                "MACRO_RISK_OFF_DEFENSIVE",
                4.0,
                "Makro robi się defensywne: equities korygują, dolar mocniejszy, VIX podwyższony.",
            )

        if us10y_week is not None and us10y_week > 5.0 and eq_score is not None and eq_score < 0.0:
            add(
                "MACRO_RISK_OFF_DEFENSIVE",
                2.0,
                "Rentowności US10Y rosną przy słabszych equities.",
            )

        # ------------------------
        # BEAR_TREND_DEFENSIVE
        # ------------------------
        avg_btc_pct = None

        if all(isinstance(x, (int, float)) for x in (btc_m, btc_w, btc_d)):
            avg_btc_pct = (btc_m + btc_w + btc_d) / 3.0

        if btc_score <= -4.0 and sol_score <= -3.0:
            add(
                "BEAR_TREND_DEFENSIVE",
                7.0,
                "BTC i SOL są w wyraźnie spadkowym układzie wielookresowym.",
            )
        elif (
            btc_m is not None
            and btc_w is not None
            and btc_d is not None
            and btc_m < 0
            and btc_w < 0
            and btc_d < 0
        ):
            if avg_btc_pct is not None and avg_btc_pct <= -3.0:
                add(
                    "BEAR_TREND_DEFENSIVE",
                    6.0,
                    "BTC spadkowy na wszystkich interwałach, a ruch jest znaczący.",
                )
            elif avg_btc_pct is not None and avg_btc_pct <= -1.0:
                add(
                    "BEAR_TREND_DEFENSIVE",
                    4.0,
                    "BTC spadkowy na wszystkich interwałach.",
                )
            else:
                add(
                    "BEAR_TREND_DEFENSIVE",
                    2.0,
                    "BTC lekko spadkowy na wszystkich interwałach, ale bez silnej dynamiki.",
                )

        if solbtc_score <= -2.0:
            add(
                "BEAR_TREND_DEFENSIVE",
                2.0,
                "SOL/BTC słabnie — SOL traci względem BTC.",
            )

        # ------------------------
        # BEAR_BOUNCE_CAUTIOUS
        # ------------------------
        if (
            btc_m is not None
            and btc_w is not None
            and btc_d is not None
            and btc_m < -1.0
            and btc_w > 0.5
            and btc_d < 0.0
        ):
            add(
                "BEAR_BOUNCE_CAUTIOUS",
                6.0,
                "Miesięczny trend spadkowy BTC, tygodniowe odbicie, ale dzienna słabość.",
            )
        elif btc_score < 0.0 and sol_score > btc_score and (solbtc_w or 0.0) > 0.0:
            add(
                "BEAR_BOUNCE_CAUTIOUS",
                2.0,
                "Odbicie SOL/BTC w słabszym otoczeniu BTC.",
            )

        # ------------------------
        # BULL_TREND_MOMENTUM
        # ------------------------
        if btc_score >= 4.0 and sol_score >= 3.0:
            add(
                "BULL_TREND_MOMENTUM",
                7.0,
                "BTC i SOL mają wyraźne momentum wzrostowe.",
            )

            if eq_score is not None and eq_score >= 0.0:
                add(
                    "BULL_TREND_MOMENTUM",
                    2.0,
                    "Equities potwierdzają risk-on.",
                )
        elif btc_score >= 2.0 and sol_score >= 2.0 and solbtc_score >= -1.0:
            add(
                "BULL_TREND_MOMENTUM",
                3.0,
                "Crypto w łagodnym trendzie wzrostowym.",
            )

        # ------------------------
        # BULL_PULLBACK_CONSTRUCTIVE
        # ------------------------
        if (
            btc_m is not None
            and btc_w is not None
            and btc_d is not None
            and btc_m > 0.0
            and btc_w > 0.0
            and btc_d < 0.0
            and abs_val(btc_d) < 2.0
        ):
            add(
                "BULL_PULLBACK_CONSTRUCTIVE",
                6.0,
                "BTC w trendzie średnioterminowym, dzienna korekta wygląda konstruktywnie.",
            )

        if (
            sol_m is not None
            and sol_w is not None
            and sol_d is not None
            and sol_m > 0.0
            and sol_w > 0.0
            and sol_d < 0.0
            and abs_val(sol_d) < 3.0
        ):
            add(
                "BULL_PULLBACK_CONSTRUCTIVE",
                3.0,
                "SOL w trendzie średnioterminowym, dzienna korekta.",
            )

        # ------------------------
        # BTC_LEADERSHIP_ALTS_WEAK
        # ------------------------
        if btc_score > 0.0 and solbtc_score < -1.0 and sol_score < btc_score:
            add(
                "BTC_LEADERSHIP_ALTS_WEAK",
                7.0,
                "BTC mocniejszy niż SOL/BTC — alty są słabe względem BTC.",
            )
        elif btc_score >= 0.0 and solbtc_w is not None and solbtc_w < -1.0:
            add(
                "BTC_LEADERSHIP_ALTS_WEAK",
                3.0,
                "SOL/BTC osłabia się względem BTC.",
            )

        # ------------------------
        # ALTSEASON_ROTATION
        # ------------------------
        ethbtc_week = self._pct(ethbtc.get("week", {}))

        if ethbtc_week is None:
            eth_w = self._pct(eth.get("week", {}))
            if eth_w is not None and btc_w is not None:
                ethbtc_week = eth_w - btc_w

        if solbtc_score >= 2.0 and (solbtc_w or 0.0) > 1.0 and btc_score > -2.0:
            add(
                "ALTSEASON_ROTATION",
                7.0,
                "SOL/BTC odzyskuje siłę przy stabilnym BTC — możliwa rotacja w alty.",
            )

        if ethbtc_week is not None and ethbtc_week > 2.0 and (solbtc_w or 0.0) > 1.5:
            add(
                "ALTSEASON_ROTATION",
                4.0,
                "ETH i SOL outperformują BTC — sygnał rotacji altseasonowej.",
            )

        # ------------------------
        # SOL_RECOVERY_SELECTIVE
        # ------------------------
        if (
            (solbtc_w or 0.0) > 1.0
            and sol_w is not None
            and btc_w is not None
            and sol_w > btc_w
            and (btc_score <= 0.0 or (solbtc_m or 0.0) <= 0.0)
        ):
            add(
                "SOL_RECOVERY_SELECTIVE",
                6.0,
                "SOL outperformuje BTC, ale BTC/SOL-BTC nie jest w pełnym byczym trendzie — selektywne odbicie.",
            )

        if (solbtc_d or 0.0) < -0.7 and scores["SOL_RECOVERY_SELECTIVE"] > 0.0:
            add(
                "SOL_RECOVERY_SELECTIVE",
                -1.0,
                "Dzienna korekta SOL/BTC obniża pewność odbicia.",
            )

        # ------------------------
        # RANGE_COMPRESSION_WAIT
        # ------------------------
        if (
            btc_day_range is not None
            and btc_day_range < 2.0
            and abs_val(btc_d) < 1.0
            and abs_val(btc_w) < 2.5
        ):
            add(
                "RANGE_COMPRESSION_WAIT",
                5.0,
                "BTC jest w wąskim zakresie i małych zmianach — kompresja.",
            )

        if (
            sol_day_range is not None
            and sol_day_range < 2.5
            and abs_val(sol_d) < 2.0
            and abs_val(sol_w) < 3.5
        ):
            add(
                "RANGE_COMPRESSION_WAIT",
                2.0,
                "SOL również jest w niskiej zmienności.",
            )

        # ------------------------
        # RANGE_CHOP_SELECTIVE
        # ------------------------
        if (
            -3.0 <= btc_score <= 3.0
            and -3.0 <= sol_score <= 3.0
            and (btc_day_range or 0.0) < 3.5
            and scores["RANGE_COMPRESSION_WAIT"] < 3.0
        ):
            add(
                "RANGE_CHOP_SELECTIVE",
                4.0,
                "Brak wyraźnego trendu, ale zmienność nie wskazuje na kompresję — chop.",
            )

        if solbtc_score is not None and -2.0 <= solbtc_score <= 2.0 and abs_val(solbtc_w) < 2.5:
            add(
                "RANGE_CHOP_SELECTIVE",
                1.0,
                "SOL/BTC miesza się w zakresie.",
            )

        # ------------------------
        # Korekty końcowe
        # ------------------------
        if scores["SOL_RECOVERY_SELECTIVE"] >= 4.0 or scores["ALTSEASON_ROTATION"] >= 4.0:
            add(
                "RANGE_COMPRESSION_WAIT",
                -2.0,
                "Silna relatywna siła SOL/BTC obniża wagę czystej kompresji.",
            )

        stress_penalty = max(scores["HIGH_VOL_STRESS"], scores["MACRO_RISK_OFF_DEFENSIVE"])

        if stress_penalty >= 5.0:
            offensive_profiles = [
                "BULL_TREND_MOMENTUM",
                "BULL_PULLBACK_CONSTRUCTIVE",
                "ALTSEASON_ROTATION",
                "SOL_RECOVERY_SELECTIVE",
            ]

            for profile in offensive_profiles:
                if scores[profile] > 0:
                    add(
                        profile,
                        -scores[profile] * 0.5,
                        "Wysoki stres makro/zmienność obniża sygnały ofensywne.",
                    )

        ranked = sorted(
            scores.items(),
            key=lambda item: (
                -item[1],
                PROFILE_PRIORITY.index(item[0])
                if item[0] in PROFILE_PRIORITY
                else len(PROFILE_PRIORITY),
            ),
        )

        chosen, chosen_score = ranked[0]

        if chosen_score <= 0.75:
            chosen = "FALLBACK_NEUTRAL"
            chosen_score = scores[chosen]

        eq_text = f"{eq_score:+.1f}" if isinstance(eq_score, (int, float)) else "brak"

        lines = [
            f"Sugerowany profil: {chosen}",
            f"Siła sygnału: {chosen_score:.2f}",
            "",
            "Najwyżej ocenione profile:",
        ]

        for name, score in ranked[:3]:
            lines.append(f"- {name}: {score:.2f}")

        lines.extend(
            [
                "",
                "Kluczowe metryki:",
                f"BTC Mies./Tyg./Dzień: {fmt_pct(btc_m)} / {fmt_pct(btc_w)} / {fmt_pct(btc_d)}",
                f"SOL Mies./Tyg./Dzień: {fmt_pct(sol_m)} / {fmt_pct(sol_w)} / {fmt_pct(sol_d)}",
                f"SOL/BTC Mies./Tyg./Dzień: {fmt_pct(solbtc_m)} / {fmt_pct(solbtc_w)} / {fmt_pct(solbtc_d)}",
                f"Trend score BTC/SOL/SOL-BTC: {btc_score:+.1f} / {sol_score:+.1f} / {solbtc_score:+.1f}",
                f"Equities score: {eq_text}",
                f"VIX cena: {fmt_num(vix_price)}",
                f"Zmienność dzienna BTC/SOL: {fmt_num(btc_day_range)}% / {fmt_num(sol_day_range)}%",
                "",
                "Powody dla wybranego profilu:",
            ]
        )

        if reasons[chosen]:
            for reason in reasons[chosen]:
                lines.append(f"- {reason}")
        else:
            lines.append("- Brak szczegółowych powodów.")

        return {
            "profile": chosen,
            "score": chosen_score,
            "reasons": reasons[chosen],
            "lines": lines,
        }

    def show_profile_suggestion(self):
        try:
            result = self._compute_profile_suggestion()
        except Exception as e:
            QMessageBox.critical(
                self,
                "Błąd",
                f"Nie udało się obliczyć profilu:\n\n{str(e)}",
            )
            return

        text = "\n".join(result["lines"])

        try:
            os.makedirs(TARGET_DIRECTORY, exist_ok=True)
            profile_path = os.path.join(TARGET_DIRECTORY, "profil_dla_ai.md")

            with open(profile_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")

            self.status_label.setText(f"Zapisano sugestię profilu: {profile_path}")

        except Exception:
            self.status_label.setText(
                "Sugestia profilu obliczona, ale nie udało się zapisać pliku profil_dla_ai.md."
            )

        QMessageBox.information(self, "Sugestia profilu SOL/BTC", text)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    app.setStyleSheet(
    """
    /* Ustawienie domyślnego rozmiaru i kroju czcionki dla całego interfejsu */
    QWidget {
        font-size: 16px;
        color: #c9d1d9;
    }

    QMainWindow {
        background: #121212;
    }
    QGroupBox {
        font-weight: bold;
        border: 1px solid #2d3139;
        border-radius: 8px;
        margin-top: 14px;
        padding: 10px 8px 8px 8px;
        color: #e6edf3;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0px 5px;
        color: #58a6ff;
    }
    QPushButton {
        padding: 8px 12px;
        border-radius: 6px;
        border: 1px solid #3d444d;
        background: #21262d;
    }
    QPushButton:hover {
        background: #30363d;
        border-color: #8b949e;
    }
    QPushButton:pressed {
        background: #161b22;
    }
    QScrollArea {
        border: 1px solid #2d3139;
        border-radius: 6px;
        background: #121212;
    }
    /* QLabel zachowuje swój rozmiar lub można go usunąć, by dziedziczył z QWidget */
    QLabel {
        font-size: 14px;
    }
    QCheckBox {
        spacing: 6px;
    }
    """
)


    window = MainWindow()
    window.show()

    sys.exit(app.exec_())