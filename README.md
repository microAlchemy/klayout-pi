# klayout-pi
Power Integrity tools for KLayout 

## Development workflow

### Test the macro end-to-end

symlink dev
```sh
ln -s /path/to/klayout-pi $KLAYOUT_HOME/salt/klayout-pi
```
Now edits in this repo are picked up by KLayout immediately on macro reload.


### Regenerate the test fixture

If you change kpex or the test layout:
```sh
kpex --pdk ihp-sg13g2 --magic --magic_mode R \
     --gds  tests/inverter_simple.gds --cell TOP \
     --out_dir   /tmp/inverter_pex \
     --out_spice tests/inverter_simple.R.spice
```

## Prereqs (not in the repo)

- KLayout 0.28+ (tested on 0.30.8)
- conda env with `kpex` installed (or any other source of the CLI on PATH)
- `magic` (8.3+)
- `ngspice` (any reasonably recent)
- IHP SG13G2 PDK (via `ciel enable ihp-sg13g2` or equivalent)
