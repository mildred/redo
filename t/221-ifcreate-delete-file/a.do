if [ -e b ]; then
  redo-ifchange b
else
  redo-ifcreate b
fi
echo $$ >$3
echo $$ >>log
