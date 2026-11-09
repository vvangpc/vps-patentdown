#!/bin/sh
set -e
/usr/sbin/nginx -t
/bin/systemctl reload nginx
