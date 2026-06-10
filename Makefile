obj-m += conveyor_node.o

KDIR ?= $(HOME)/linux-rpi
PWD := $(shell pwd)
APP := conveyor_monitor
RPI ?= pi@10.10.10.12
RPI_DIR ?= ~/conveyor_node_driver
BROKER_RPI ?= pi@10.10.11.12
BROKER_DIR ?= ~/conveyor_broker
CC := $(CROSS_COMPILE)gcc

.PHONY: all modules app clean scp deploy

all: modules app

modules:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

app:
	$(CC) -Wall -Wextra -O2 -o $(APP) conveyor_monitor.c

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f $(APP)

scp:
	ssh $(RPI) "mkdir -p $(RPI_DIR)"
	scp conveyor_node.ko $(APP) conveyor_event_daemon.py mknod.sh README.md $(RPI):$(RPI_DIR)/

deploy:
	chmod +x deploy.sh
	DRIVER_PI=$(RPI) BROKER_PI=$(BROKER_RPI) bash deploy.sh
