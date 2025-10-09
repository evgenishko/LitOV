"""GUI application for controlling an Ocean Optics FLAME-S-UV-VIS spectrometer.

The application is written with PySide6 and seabreeze.  It provides a left panel
with acquisition controls and a right panel that visualises the measured
spectrum.  Users can perform one-off captures, start or stop continuous
acquisition, and save the most recently captured spectrum to CSV.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
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
        """Ensure intensities are positive for logarithmic plotting."""

        # Matplotlib cannot display non-positive values on a log scale.  Clamp such values
        # to a small positive floor so that the plot remains meaningful while preserving the
        # order of magnitude for positive intensities.
        floor = 1e-6
        adjusted = []
        for value in intensities:
            if value > floor:
                adjusted.append(value)
            else:
                adjusted.append(floor)
        return tuple(adjusted)


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

        self._start_button = QPushButton("Начать съёмку")
        self._start_button.clicked.connect(self._toggle_acquisition)

        self._snapshot_button = QPushButton("Сделать снимок")
        self._snapshot_button.clicked.connect(self._capture_single)

        self._save_button = QPushButton("Сохранить в .CSV")
        self._save_button.clicked.connect(self._save_csv)

        self._status_label = QLabel("Статус: ожидается подключение")

        layout.addWidget(integration_label, 0, 0)
        layout.addWidget(self._integration_spin, 0, 1)
        layout.addWidget(interval_label, 1, 0)
        layout.addWidget(self._interval_spin, 1, 1)
        layout.addWidget(self._start_button, 2, 0, 1, 2)
        layout.addWidget(self._snapshot_button, 3, 0, 1, 2)
        layout.addWidget(self._save_button, 4, 0, 1, 2)
        layout.addWidget(self._status_label, 5, 0, 1, 2)

        layout.setRowStretch(6, 1)

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

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._snapshot_button.setEnabled(enabled)
        self._save_button.setEnabled(enabled)
        self._integration_spin.setEnabled(enabled)
        self._interval_spin.setEnabled(enabled)

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

    def _on_spectrum_ready(self, spectrum: Spectrum) -> None:
        self._current_spectrum = spectrum
        self._canvas.update_spectrum(spectrum)

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
