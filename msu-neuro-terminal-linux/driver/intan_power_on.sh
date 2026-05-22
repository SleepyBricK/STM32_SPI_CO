#!/bin/bash
# Включение питания Intan через GPIO 226 (PH2)

GPIO=226

# Экспорт GPIO
echo "$GPIO" | sudo tee /sys/class/gpio/export 2>/dev/null
sleep 0.2

# Направление: выход
echo "out" | sudo tee /sys/class/gpio/gpio${GPIO}/direction

# Включить питание (1)
echo "1" | sudo tee /sys/class/gpio/gpio${GPIO}/value

echo "Питание Intan включено (GPIO $GPIO)"
sleep 0.2
echo "Теперь можно читать /dev/intan"
