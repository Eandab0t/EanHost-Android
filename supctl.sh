grepsup() { grep -l svc/supervisor.sh /proc/[0-9]*/cmdline 2>/dev/null; }
echo "before:"
grepsup
for f in $(grepsup); do
  p=${f#/proc/}; p=${p%/cmdline}
  [ "$p" = "$$" ] && continue
  kill $p 2>/dev/null && echo "killed $p"
done
rm -rf /sdcard/EanHost/sup.lock
sleep 1
echo "after-kill:"
grepsup
sh /sdcard/EanHost/svc/sstart.sh
sleep 3
echo "after-start:"
grepsup
echo "sup.pid: $(cat /sdcard/EanHost/sup.pid)"
l=; read l x < /sdcard/EanHost/sup.lock/pid 2>/dev/null; echo "lockowner=$l"
sh /sdcard/EanHost/svc/sstart.sh; sh /sdcard/EanHost/svc/sstart.sh
sleep 2
echo "sup.pid after 3x sstart: $(cat /sdcard/EanHost/sup.pid)"
echo "supervisors after 3x sstart:"; grepsup