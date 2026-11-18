@echo off
chcp 65001 >nul
rem Весь файл ниже - один блок в скобках: Windows читает его целиком сразу,
rem поэтому автообновление может безопасно заменить этот файл, пока он работает.
(
  cd /d "%~dp0"
  title Криптобот: главное окно
  echo === Криптобот: запуск ботов на демо-счёте Bybit ===
  where py >nul 2>nul
  if errorlevel 1 (
    echo Не найден Python. Скачайте Python 3.12 с https://www.python.org/downloads/release/python-3128/ и запустите этот файл снова.
    pause
    exit /b
  )
  py -3.12 -c "print('Python 3.12 OK')" >nul 2>nul
  if errorlevel 1 py install 3.12
  py -3.12 -c "import pybit, pandas, yaml, Crypto" >nul 2>nul
  if errorlevel 1 (
    echo Устанавливаю библиотеки, пару минут...
    py -3.12 -m pip install -r requirements.txt
  )
  py -3.12 keys.py
  if errorlevel 1 (
    pause
    exit /b
  )
  echo.
  echo Проверяю обновления и запускаю два бота: спот BTC и фьючерсы. Их окна откроются отдельно.
  echo ЭТО окно не закрывайте: оно следит за ботами и раз в 6 часов само ставит обновления от Криптобота.
  echo Чтобы остановить всё: закройте это окно, потом окна обоих ботов.
  echo.
  py -3.12 updater.py --run
  echo.
  echo Главное окно остановилось. Сделайте скриншот и пришлите Криптоботу.
  pause
  exit /b
)
