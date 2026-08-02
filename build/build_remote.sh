#!/bin/bash

echo "========================================"
echo "Building Irudo Remote Agent for Linux"
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

# Build the headless remote agent (no web / LLM / third-party deps)
echo "Building..."
pyinstaller --onefile --name irudo_remote \
    --console \
    remote/main.py

if [ $? -eq 0 ]; then
    echo
    echo "========================================"
    echo "Build completed successfully!"
    echo "Executable: dist/irudo_remote"
    echo "========================================"

    # Move executable to build directory
    mv dist/irudo_remote build/ 2>/dev/null

    # Make the executable executable
    chmod +x build/irudo_remote

    echo "Executable moved to: build/irudo_remote"
    echo "File permissions set to executable"
    echo
    echo "Usage:"
    echo "  build/irudo_remote --c2-address 192.168.1.100:8881 --agent-id BOT-01 --auth-token token-for-server-01"
    echo "  build/irudo_remote --config config_remote.json"
    echo
    echo "NOTE: pass --config with an absolute path to your config_remote.json"
    echo "      (a config is NOT embedded into the executable)."
else
    echo
    echo "Build failed!"
    exit 1
fi

echo
read -p "Press Enter to continue..."
