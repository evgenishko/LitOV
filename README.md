# LitOV

Простое приложение на PySide6 для работы со спектрометром Ocean Optics FLAME-S-UV-VIS.

## Запуск

1. Установите зависимости:

   ```bash
   pip install pyside6 matplotlib seabreeze
   ```

2. Подключите спектрометр и убедитесь, что драйверы установлены корректно. На Windows
   после установки пакета `seabreeze` выполните настройку DLL командой:

   ```powershell
   python -m seabreeze.os_setup
   ```

   Эта команда скопирует `SeaBreeze.dll` в папку Python, что необходимо для
   обнаружения устройств. Для проверки можно запустить:

   ```powershell
   python -c "from seabreeze.spectrometers import list_devices; print(list_devices())"
   ```

   Если список не пустой, приложение сможет подключиться к спектрометру.

3. Запустите графический интерфейс:

   ```bash
   python app.py
   ```

## Сборка исполняемого файла под Windows

1. Установите `pyinstaller` (при необходимости активируйте виртуальное окружение):

   ```powershell
   pip install pyinstaller
   ```

2. Соберите исполняемый файл без консольного окна:

   ```powershell
   pyinstaller --noconsole --onefile --name LitOV app.py
   ```

   Готовый файл появится в каталоге `dist`.

## Возможности

- Управление временем интеграции и интервалом непрерывной съёмки.
- Одиночный снимок и непрерывная съёмка с заданным интервалом.
- Сохранение последнего спектра в CSV-файл формата `"{wl_nm:.6f}, {abs(cur)}"`.
