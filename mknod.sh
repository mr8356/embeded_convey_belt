#!/bin/sh

echo "conveyor_node: no manual mknod is required."
echo "The module registers /dev/conveyor_node0 automatically through miscdevice."
echo "Check after module load:"
echo "  ls -l /dev/conveyor_node0"
echo "  cat /proc/conveyor_node_stats"
