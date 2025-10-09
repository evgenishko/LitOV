"""GUI application for controlling an Ocean Optics FLAME-S-UV-VIS spectrometer.

The application is written with PySide6 and seabreeze.  It provides a left panel
with acquisition controls and a right panel that visualises the measured
spectrum.  Users can perform one-off captures, start or stop continuous
acquisition, and save the most recently captured spectrum to CSV.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

# The matplotlib import needs to happen after a compatible backend is selected.
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure


try:  # pragma: no cover - hardware interaction is not easily tested
    from seabreeze.spectrometers import Spectrometer, list_devices
except Exception:  # noqa: BLE001 - we want to catch everything import related
    try:  # pragma: no cover - attempt to fall back to the pure python backend
        from seabreeze import seabreeze

        seabreeze.use("pyseabreeze")
        from seabreeze.spectrometers import Spectrometer, list_devices
    except Exception:
        Spectrometer = None  # type: ignore[assignment]
        list_devices = lambda: ()  # type: ignore[assignment]


class SpectrometerError(RuntimeError):
    """Raised when there is an issue interacting with the spectrometer."""


@dataclass
class Spectrum:
    wavelengths: Tuple[float, ...]
    intensities: Tuple[float, ...]


class SpectrometerManager(QObject):
    """Wrapper around seabreeze's Spectrometer with convenience helpers."""

    def __init__(self) -> None:
        super().__init__()
        self._spectrometer = None

    @property
    def is_connected(self) -> bool:
        return self._spectrometer is not None

    def connect(self) -> None:
        if Spectrometer is None:
            raise SpectrometerError(
                "Библиотека seabreeze недоступна. Установите seabreeze и драйверы."
            )

        try:
            devices = list(list_devices())
        except Exception as exc:  # pragma: no cover - hardware specific
            raise SpectrometerError(
                "Не удалось определить список устройств. Проверьте установку seabreeze."
            ) from exc

        if not devices:
            raise SpectrometerError(
                "Спектрометр не найден. Убедитесь, что устройство подключено и выполнена "
                "команда 'python -m seabreeze.os_setup'."
            )

        try:
            self._spectrometer = Spectrometer.from_first_available()
        except Exception as exc:  # pragma: no cover - hardware specific
            raise SpectrometerError(
                "Не удалось подключиться к спектрометру. Проверьте права доступа и драйверы."
            ) from exc

    def disconnect(self) -> None:
        if self._spectrometer is not None:
            try:
                self._spectrometer.close()  # pragma: no cover
            finally:
                self._spectrometer = None

    def integration_time_micros(self, value: int) -> None:
        if self._spectrometer is None:
            raise SpectrometerError("Спектрометр не подключен.")
        self._spectrometer.integration_time_micros(value)

    def capture(self) -> Spectrum:
        if self._spectrometer is None:
            raise SpectrometerError("Спектрометр не подключен.")

        wavelengths = tuple(float(v) for v in self._spectrometer.wavelengths())
        intensities = tuple(float(v) for v in self._spectrometer.intensities())
        return Spectrum(wavelengths, intensities)


class AcquisitionWorker(QThread):
    """Performs repeated acquisitions in the background."""

    spectrum_ready = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, manager: SpectrometerManager) -> None:
        super().__init__()
        self._manager = manager
        self._interval = 1.0
        self._running = False
        self._capture_lock = Lock()

    def start_acquisition(self, interval_seconds: float) -> None:
        self._interval = max(0.05, float(interval_seconds))
        self._running = True
        if not self.isRunning():
            self.start()

    def stop_acquisition(self) -> None:
        self._running = False

    @property
    def is_acquiring(self) -> bool:
        return self._running

    def run(self) -> None:  # pragma: no cover - thread interaction is complex
        while not self.isInterruptionRequested():
            if not self._running:
                time.sleep(0.05)
                continue

            try:
                with self._capture_lock:
                    spectrum = self._manager.capture()
            except SpectrometerError as exc:
                self.error_occurred.emit(str(exc))
                self._running = False
                continue

            self.spectrum_ready.emit(spectrum)

            # sleep respecting the interval while allowing thread stoppage
            elapsed = 0.0
            while self._running and elapsed < self._interval:
                time.sleep(min(0.1, self._interval - elapsed))
                elapsed += min(0.1, self._interval - elapsed)

    def shutdown(self) -> None:
        self._running = False
        self.requestInterruption()

    def wait_for_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until no capture is in progress in the worker thread."""

        if timeout is None:
            self._capture_lock.acquire()
            self._capture_lock.release()
            return True

        acquired = self._capture_lock.acquire(timeout=timeout)
        if acquired:
            self._capture_lock.release()
        return acquired


class SpectrumCanvas(FigureCanvasQTAgg):
    """Matplotlib canvas specialised for spectrum visualisation."""

    def __init__(self) -> None:
        self._figure = Figure(figsize=(6, 4))
        super().__init__(self._figure)
        self._axes = self._figure.add_subplot(111)
        self._axes.set_xlabel("Длина волны, нм")
        self._axes.set_ylabel("Интенсивность (логарифмическая шкала)")
        self._axes.set_yscale("log")
        self._axes.grid(True)
        self._line = None

    def update_spectrum(self, spectrum: Spectrum) -> None:
        intensities = self._sanitize_intensities(spectrum.intensities)
        if self._line is None:
            (self._line,) = self._axes.plot(
                spectrum.wavelengths, intensities, color="tab:blue"
            )
        else:
            self._line.set_data(spectrum.wavelengths, intensities)

        self._axes.relim()
        self._axes.autoscale_view()
        self.draw_idle()

    @staticmethod
    def _sanitize_intensities(intensities: Tuple[float, ...]) -> Tuple[float, ...]:
        """Clamp intensities to be compatible with the log-scaled Y axis."""

        # Требование интерфейса: отображать максимум между 1 и измеренной интенсивностью.
        # Это гарантирует корректную работу логарифмической шкалы и предотвращает
        # отображение нулей и отрицательных значений.
        floor = 1.0
        return tuple(max(floor, value) for value in intensities)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ocean Optics FLAME-S-UV-VIS")
        self.resize(1200, 700)

        self._manager = SpectrometerManager()
        self._worker = AcquisitionWorker(self._manager)
        self._worker.spectrum_ready.connect(self._on_spectrum_ready)
        self._worker.error_occurred.connect(self._on_worker_error)

        self._current_spectrum: Optional[Spectrum] = None
        self._background_spectrum: Optional[Spectrum] = None

        self._central_widget = QWidget()
        self.setCentralWidget(self._central_widget)

        main_layout = QHBoxLayout(self._central_widget)

        self._controls = self._build_controls()
        main_layout.addWidget(self._controls, 1)

        self._canvas = SpectrumCanvas()
        main_layout.addWidget(self._canvas, 2)

        self._create_menu()
        self._attempt_connect()

    # ------------------------------------------------------------------ UI --
    def _build_controls(self) -> QWidget:
        box = QGroupBox("Настройки съёмки")
        layout = QGridLayout(box)

        integration_label = QLabel("Время интеграции, мс:")
        self._integration_spin = QSpinBox()
        self._integration_spin.setRange(1, 10_000)
        self._integration_spin.setValue(100)
        self._integration_spin.valueChanged.connect(self._on_integration_changed)

        interval_label = QLabel("Интервал съёмки, с:")
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 3600)
        self._interval_spin.setValue(1)

        series_count_label = QLabel("Количество измерений:")
        self._series_count_spin = QSpinBox()
        self._series_count_spin.setRange(1, 10_000)
        self._series_count_spin.setValue(10)

        series_path_label = QLabel("Папка для серии:")
        self._series_path_edit = QLineEdit(os.getcwd())
        self._series_path_button = QPushButton("Выбрать...")
        self._series_path_button.clicked.connect(self._choose_series_directory)

        self._background_button = QPushButton("Измерить фон")
        self._background_button.clicked.connect(self._capture_background)

        self._start_button = QPushButton("Начать съёмку")
        self._start_button.clicked.connect(self._toggle_acquisition)

        self._snapshot_button = QPushButton("Сделать снимок")
        self._snapshot_button.clicked.connect(self._capture_single)

        self._save_button = QPushButton("Сохранить в .CSV")
        self._save_button.clicked.connect(self._save_csv)

        self._series_button = QPushButton("Измерить серию")
        self._series_button.clicked.connect(self._capture_series)

        self._status_label = QLabel("Статус: ожидается подключение")

        layout.addWidget(integration_label, 0, 0)
        layout.addWidget(self._integration_spin, 0, 1)
        layout.addWidget(interval_label, 1, 0)
        layout.addWidget(self._interval_spin, 1, 1)
        layout.addWidget(series_count_label, 2, 0)
        layout.addWidget(self._series_count_spin, 2, 1)
        layout.addWidget(series_path_label, 3, 0)
        layout.addWidget(self._series_path_edit, 3, 1)
        layout.addWidget(self._series_path_button, 4, 0, 1, 2)
        layout.addWidget(self._background_button, 5, 0, 1, 2)
        layout.addWidget(self._start_button, 6, 0, 1, 2)
        layout.addWidget(self._snapshot_button, 7, 0, 1, 2)
        layout.addWidget(self._save_button, 8, 0, 1, 2)
        layout.addWidget(self._series_button, 9, 0, 1, 2)
        layout.addWidget(self._status_label, 10, 0, 1, 2)

        layout.setRowStretch(11, 1)

        return box

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Файл")
        reconnect_action = QAction("Переподключить", self)
        reconnect_action.triggered.connect(self._attempt_connect)
        file_menu.addAction(reconnect_action)

        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # -------------------------------------------------------------- actions --
    def _attempt_connect(self) -> None:
        self._worker.stop_acquisition()
        self._start_button.setText("Начать съёмку")
        try:
            self._manager.connect()
        except SpectrometerError as exc:
            self._status_label.setText(f"Статус: {exc}")
            self._set_controls_enabled(False)
            return

        self._status_label.setText("Статус: спектрометр подключён")
        self._set_controls_enabled(True)
        self._apply_integration_time()
        self._background_spectrum = None

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._snapshot_button.setEnabled(enabled)
        self._save_button.setEnabled(enabled)
        self._background_button.setEnabled(enabled)
        self._integration_spin.setEnabled(enabled)
        self._interval_spin.setEnabled(enabled)
        self._series_count_spin.setEnabled(enabled)
        self._series_path_edit.setEnabled(enabled)
        self._series_path_button.setEnabled(enabled)
        self._series_button.setEnabled(enabled)

    def _on_integration_changed(self, value: int) -> None:
        if not self._manager.is_connected:
            return

        try:
            self._manager.integration_time_micros(value * 1000)
        except SpectrometerError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))

    def _apply_integration_time(self) -> None:
        self._on_integration_changed(self._integration_spin.value())

    def _toggle_acquisition(self) -> None:
        if self._worker.is_acquiring:
            self._worker.stop_acquisition()
            self._start_button.setText("Начать съёмку")
            self._status_label.setText("Статус: непрерывная съёмка остановлена")
            return

        interval = self._interval_spin.value()
        self._worker.start_acquisition(interval)
        self._start_button.setText("Остановить съёмку")
        self._status_label.setText("Статус: идёт непрерывная съёмка")

    def _capture_single(self) -> None:
        try:
            spectrum = self._manager.capture()
        except SpectrometerError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return

        self._on_spectrum_ready(spectrum)
        self._status_label.setText("Статус: получен одиночный снимок")

    def _capture_background(self) -> None:
        was_running = self._worker.is_acquiring
        if was_running:
            self._worker.stop_acquisition()
            self._start_button.setText("Начать съёмку")
            if not self._worker.wait_for_idle(timeout=5.0):
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    "Не удалось дождаться завершения текущей съёмки.",
                )
                return

        try:
            spectrum = self._manager.capture()
        except SpectrometerError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return

        self._background_spectrum = spectrum
        self._status_label.setText("Статус: фон измерен и будет вычитаться")

        if was_running:
            self._status_label.setText(
                "Статус: фон измерен; перезапустите съёмку для обновления данных"
            )

    def _save_csv(self) -> None:
        if self._current_spectrum is None:
            QMessageBox.information(self, "Нет данных", "Сначала выполните съёмку.")
            return

        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить спектр",
            "spectrum.csv",
            "CSV Files (*.csv)",
        )

        if not filename:
            return

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("wavelength_nm, counts\n")
                for wl, cur in zip(
                    self._current_spectrum.wavelengths,
                    self._current_spectrum.intensities,
                ):
                    f.write(f"{wl:.6f}, {abs(cur)}\n")
        except OSError as exc:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить файл: {exc}")
            return

        self._status_label.setText(f"Статус: спектр сохранён в {filename}")

    def _choose_series_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Выбрать папку для серии",
            self._series_path_edit.text() or os.getcwd(),
        )
        if directory:
            self._series_path_edit.setText(directory)

    def _capture_series(self) -> None:
        count = self._series_count_spin.value()
        target_dir = self._series_path_edit.text().strip() or os.getcwd()

        was_running = self._worker.is_acquiring
        if was_running:
            self._worker.stop_acquisition()
            self._start_button.setText("Начать съёмку")
            if not self._worker.wait_for_idle(timeout=5.0):
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    "Не удалось дождаться завершения текущей съёмки.",
                )
                return

        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Ошибка",
                f"Не удалось создать папку для сохранения: {exc}",
            )
            return

        interval_seconds = self._interval_spin.value()

        self._series_button.setEnabled(False)
        self._status_label.setText("Статус: выполняется серия измерений")
        QApplication.processEvents()

        for index in range(count):
            try:
                if not self._worker.wait_for_idle(timeout=5.0):
                    raise SpectrometerError(
                        "Рабочий поток занят. Серия остановлена."
                    )
                spectrum = self._manager.capture()
            except SpectrometerError as exc:
                QMessageBox.warning(self, "Ошибка", str(exc))
                self._series_button.setEnabled(True)
                self._status_label.setText("Статус: серия измерений прервана")
                return

            processed = self._subtract_background(spectrum)
            filename = os.path.join(target_dir, f"sample{index}.csv")
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    for wl, cur in zip(processed.wavelengths, processed.intensities):
                        f.write(f"{wl:.6f},{abs(cur)}\n")
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    f"Не удалось сохранить файл {filename}: {exc}",
                )
                self._series_button.setEnabled(True)
                self._status_label.setText("Статус: серия измерений прервана")
                return

            if index < count - 1:
                time.sleep(interval_seconds)

        self._series_button.setEnabled(True)
        if was_running:
            self._status_label.setText(
                f"Статус: серия из {count} измерений сохранена в {target_dir}. "
                "Перезапустите съёмку при необходимости."
            )
        else:
            self._status_label.setText(
                f"Статус: серия из {count} измерений сохранена в {target_dir}"
            )

    def _on_spectrum_ready(self, spectrum: Spectrum) -> None:
        processed = self._subtract_background(spectrum)
        self._current_spectrum = processed
        self._canvas.update_spectrum(processed)

    def _subtract_background(self, spectrum: Spectrum) -> Spectrum:
        if self._background_spectrum is None:
            return spectrum

        background = self._background_spectrum

        if len(spectrum.wavelengths) != len(background.wavelengths):
            QMessageBox.warning(
                self,
                "Предупреждение",
                "Размер измеренного спектра не совпадает с фоном. Фон будет сброшен.",
            )
            self._background_spectrum = None
            return spectrum

        if spectrum.wavelengths != background.wavelengths:
            QMessageBox.warning(
                self,
                "Предупреждение",
                "Длины волн фона отличаются. Фон будет сброшен.",
            )
            self._background_spectrum = None
            return spectrum

        corrected_intensities = tuple(
            cur - bg for cur, bg in zip(spectrum.intensities, background.intensities)
        )

        return Spectrum(spectrum.wavelengths, corrected_intensities)

    def _on_worker_error(self, message: str) -> None:
        QMessageBox.warning(self, "Ошибка", message)
        self._start_button.setText("Начать съёмку")
        self._status_label.setText(f"Статус: {message}")

    # -------------------------------------------------------------- events --
    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._worker.shutdown()
        if self._worker.isRunning():
            self._worker.wait(1000)
        self._manager.disconnect()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
