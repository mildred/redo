. ../skip-if-minimal-do.sh

redo a
../flush-cache

:>log
rm -f a

redo-ifchange a
[ $(wc -l <log) = 1 ] || exit 11

redo-ifchange a
[ $(wc -l <log) = 1 ] || exit 12

redo b

redo-ifchange a
[ $(wc -l <log) = 2 ] || exit 13

redo-ifchange a
[ $(wc -l <log) = 2 ] || exit 14

rm -f b

redo-ifchange a
[ $(wc -l <log) = 3 ] || exit 15

redo-ifchange a
[ $(wc -l <log) = 3 ] || exit 16



