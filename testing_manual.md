# The laser machine default run on COM8 (on windows)
- and COM7 is a paired portal with COM8

# Open virtual serial paired COM4 <=> COM3
```bash
sudo socat -d -d   pty,link=/dev/ttyCOM4,raw,echo=0,perm=0666   pty,link=/dev/ttyCOM3,raw,echo=0,perm=0666
```
# Open virtual serial paired COM8 <=> COM7
```bash
sudo socat -d -d   pty,link=/dev/ttyCOM8,raw,echo=0,perm=0666   pty,link=/dev/ttyCOM7,raw,echo=0,perm=0666
```
# Steps
1. Sending trigger: from Laser
```bash
printf '2790005577,NEEDPSN12\r\n' | sudo tee /dev/ttyCOM8 > /dev/null
```

2. Sending response: from SFC
```bash
printf '2505004562,H25101801031,PASS\r\n' | sudo tee /dev/ttyCOM4 > /dev/null
```

3. Sending response: from SFC - provide DSNs for LASER to Carv
```bash
printf '2505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N8,2505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N8,PASS\r\n' | sudo tee /dev/ttyCOM4 > /dev/null
```

4. Sending result Carved: from LASER
```bash
printf '2505004562,PF2AS04TE,PASSED=1\r\n' | sudo tee /dev/ttyCOM8 > /dev/null
```
5. Finalize the result from SFC
```bash
printf '2505004562,PF2AS04TE,PASSED=1PASS\r\n' | sudo tee /dev/ttyCOM4 > /dev/null
```
