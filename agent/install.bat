@echo off
echo ===================================================
echo Instalador del Agente de Sincronizacion Eleventa
echo ===================================================
echo.

:: Comprobar si Python está instalado
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python no esta instalado o no esta en el PATH.
    echo Por favor instala Python 3.9 o superior desde python.org
    echo Asegurate de marcar la casilla "Add Python to PATH" durante la instalacion.
    pause
    exit /b 1
)

echo [OK] Python detectado.

:: Crear entorno virtual si no existe
IF NOT EXIST "venv" (
    echo Creando entorno virtual (venv)...
    python -m venv venv
) ELSE (
    echo Entorno virtual ya existe.
)

:: Activar entorno e instalar dependencias
echo Activando entorno virtual...
call venv\Scripts\activate.bat

echo.
echo Instalando dependencias (esto puede tardar unos momentos)...
pip install -r requirements.txt

echo.
:: Preparar archivo .env si no existe
IF NOT EXIST ".env" (
    echo Creando archivo de configuracion .env desde el ejemplo...
    copy .env.example .env
    echo [ATENCION] Abre el archivo .env con el bloc de notas y pon tus contrasenas reales.
) ELSE (
    echo Archivo de configuracion .env ya existe.
)

echo.
echo ===================================================
echo Instalacion Completada Exitosamente!
echo ===================================================
echo.
echo Para iniciar el agente, simplemente haz doble clic en "iniciar_agente.bat"
echo (que creare a continuacion si no existe).
echo.

IF NOT EXIST "iniciar_agente.bat" (
    echo @echo off > iniciar_agente.bat
    echo echo Iniciando Agente de Sincronizacion... >> iniciar_agente.bat
    echo call venv\Scripts\activate.bat >> iniciar_agente.bat
    echo python main.py >> iniciar_agente.bat
    echo pause >> iniciar_agente.bat
    echo [INFO] Archivo "iniciar_agente.bat" creado.
)

pause
