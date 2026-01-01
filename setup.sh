#!/bin/bash
# Arc'teryx Outlet stock watcher - setup script

echo "================================"
echo "Arc'teryx Outlet Stock Watch Setup"
echo "================================"
echo ""

# Check Python version
echo "Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.7+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Found Python $PYTHON_VERSION"
echo ""

# Create virtualenv
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
    if [ $? -eq 0 ]; then
        echo "✓ Virtual environment created"
    else
        echo "❌ Failed to create virtual environment"
        exit 1
    fi
else
    echo "✓ Virtual environment already exists"
fi
echo ""

# Activate venv and install dependencies
echo "Installing Python dependencies..."
source venv/bin/activate
pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo "✓ Dependencies installed"
else
    echo "❌ Failed to install dependencies"
    exit 1
fi
echo ""

# Create required directories
echo "Creating data/log directories..."
mkdir -p data
mkdir -p logs
echo "✓ Directories created"
echo ""

# Set execute permissions
chmod +x watch_stock.py monitor_unified.py run.sh
echo "✓ Executable permissions set"
echo ""

echo "================================"
echo "Setup complete!"
echo "================================"
echo ""
echo "Usage:"
echo "  1) Run once (defaults: men's footwear + default keywords):"
echo "     ./run.sh"
echo ""
echo "  2) Use a config file:"
echo "     cp stock_watch_config.example.json stock_watch_config.json"
echo "     ./run.sh --config stock_watch_config.json"
echo ""
echo "  3) Schedule on Linux (optional):"
echo "     See README.md (cron example)"
echo ""
echo "The first run writes a baseline in the state file."
echo "If a target size is already in stock, an alert is sent by default (disable with notify_on_first_run=false)."
echo ""
