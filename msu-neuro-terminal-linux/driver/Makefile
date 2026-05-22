# Makefile для Intan RHS2116 SPI драйвера
# Orange Pi Zero 2W, ядро 6.1.31-sun50iw9

obj-m := intan_spi.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD := $(shell pwd)

all:
	@echo "Сборка kernel модуля..."
	$(MAKE) -C $(KDIR) M=$(PWD) modules

dtbo: sun50i-h616-intan.dtbo

sun50i-h616-intan.dtbo: sun50i-h616-intan.dts
	dtc -@ -I dts -O dtb -o $@ $<

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f sun50i-h616-intan.dtbo

install: all
	$(MAKE) -C $(KDIR) M=$(PWD) modules_install
	depmod -a

# Установка без подписи (если sign-file падает с SSL ошибкой)
# Запуск: sudo make install-unsigned
install-unsigned: all
	mkdir -p /lib/modules/$(shell uname -r)/extra
	cp intan_spi.ko /lib/modules/$(shell uname -r)/extra/
	depmod -a
	@echo "Модуль установлен (без подписи)"

install-overlay: sun50i-h616-intan.dtbo
	cp sun50i-h616-intan.dtbo /boot/dtb-$(shell uname -r)/allwinner/overlay/
	@echo "Overlay скопирован. Добавьте sun50i-h616-intan в overlays в /boot/orangepiEnv.txt"

benchmark:
	@echo "Запуск SPI бенчмарка (нужен sudo)..."
	sudo python3 spi_benchmark.py

.PHONY: all clean install install-unsigned install-overlay dtbo benchmark
