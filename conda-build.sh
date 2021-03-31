#!/bin/bash

# conda install conda-build

conda build purge

rm -rfv qnt-*
rm -rfv *.gz
rm -rfv *.csv

conda-build . --no-include-recipe

PKG=`conda-build .  --output`

echo "Ready -> $PKG"

ls -lh $PKG

cp -fv $PKG .

