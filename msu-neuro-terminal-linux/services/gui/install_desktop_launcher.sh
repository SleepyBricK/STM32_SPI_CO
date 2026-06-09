#!/bin/bash
# Устанавливает ярлык запуска GUI в меню приложений и на рабочий стол.
# Запуск: bash install_desktop_launcher.sh  (или ./install_desktop_launcher.sh после chmod +x)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Делаем скрипт запуска исполняемым
chmod +x "${SCRIPT_DIR}/launch_intan_gui.sh" 2>/dev/null || true

if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: .desktop не поддерживается — создаём только .command
    DESKTOP_DIR="$HOME/Desktop"
    [ -d "$HOME/Desktop" ] || DESKTOP_DIR="$HOME/Рабочий стол"
    [ -d "$DESKTOP_DIR" ] || DESKTOP_DIR="$HOME"
    
    # Удаляем старый .desktop с рабочего стола (формат Linux, на Mac не работает)
    rm -f "$DESKTOP_DIR/intan-rhs2116-gui.desktop" 2>/dev/null || true
    
    LAUNCHER="${DESKTOP_DIR}/Intan RHS2116 Control.command"
    cat > "$LAUNCHER" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
nohup python3 intan_gui_clientv5_linux.py > /dev/null 2>&1 &
exit
EOF
    chmod +x "$LAUNCHER"
    echo "✓ Ярлык создан: $LAUNCHER"
    echo "  Запускайте GUI двойным щелчком по файлу в Finder."
else
    # Linux: .desktop в меню приложений
    DESKTOP_SRC="${SCRIPT_DIR}/intan-gui.desktop"
    APPS_DIR="${HOME}/.local/share/applications"
    DESKTOP_DEST="${APPS_DIR}/intan-rhs2116-gui.desktop"

    mkdir -p "$APPS_DIR"
    sed "s|@SCRIPT_PATH@|${SCRIPT_DIR}|g" "$DESKTOP_SRC" > "$DESKTOP_DEST"
    chmod +x "$DESKTOP_DEST"

    echo "✓ Ярлык установлен: $DESKTOP_DEST"
    echo "  GUI появится в меню приложений (категория: Наука / Электроника)"

    if [ -d "$HOME/Desktop" ] || [ -d "$HOME/Рабочий стол" ]; then
        DESKTOP_DIR="${HOME}/Desktop"
        [ -d "$HOME/Desktop" ] || DESKTOP_DIR="$HOME/Рабочий стол"
        cp "$DESKTOP_DEST" "$DESKTOP_DIR/"
        echo "✓ Ярлык также создан на рабочем столе"
    fi
fi

chmod +x "${SCRIPT_DIR}/install_desktop_launcher.sh" 2>/dev/null || true
echo ""
echo "Готово!"
