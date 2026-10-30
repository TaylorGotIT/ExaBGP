#!/bin/sh
FIFO="/var/run/exabgp.cmd"
rm -f $FIFO
mkfifo $FIFO
tail -f $FIFO
