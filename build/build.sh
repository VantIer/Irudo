#!/bin/bash

echo "========================================"
echo "Building Irudo C2 Executable for Linux"
echo "========================================"
echo

# Get script directory and go to project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR/.."

# Create build directory if not exists
if [ ! -d "build" ]; then
    mkdir -p build
fi

# Install pyinstaller if not installed
if ! python3 -c "import PyInstaller" &> /dev/null; then
    echo "Installing pyinstaller..."
    pip install pyinstaller
fi

# Check if virtual environment exists and activate if needed
if [ -d "irudoenv" ]; then
    echo "Activating virtual environment..."
    source irudoenv/bin/activate
fi

# Build the C2 executable (CLI + Web modes share this binary)
echo "Building..."
pyinstaller --onefile --name irudo_c2 \
    --add-data "web:web" \
    --hidden-import uvicorn \
    --hidden-import fastapi \
    --hidden-import openai \
    --hidden-import httpx \
    --collect-all uvicorn \
    --collect-all fastapi \
    --collect-all openai \
    --console \
    src/main.py

if [ $? -eq 0 ]; then
    echo
    echo "========================================"
    echo "Build completed successfully!"
    echo "Executable: dist/irudo_c2"
    echo "========================================"

    # Move executable to build directory
    mv dist/irudo_c2 build/ 2>/dev/null

    # Make the executable executable
    chmod +x build/irudo_c2

    echo "Executable moved to: build/irudo_c2"
    echo "File permissions set to executable"
    echo
    echo "Usage:"
    echo "  build/irudo_c2 --mode cli --config config_c2.json"
    echo "  build/irudo_c2 --mode web --config config_c2.json"
    echo
    echo "NOTE: pass --config with an absolute path to your config_c2.json"
    echo "      (a config is NOT embedded into the executable)."
else
    echo
    echo "Build failed!"
    exit 1
fi

echo
read -p "Press Enter to continue..."
