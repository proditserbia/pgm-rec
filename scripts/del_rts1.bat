::RTS1 - trazi sve fajlove koji se zavrsavaju sa .mp4 i stariji su od 30 dana i brise ih

FOR %%Z IN (.mp4) do forfiles -p D:\AutoRec\record\rts1\3_final -s -m *%%Z -d -30 -c "cmd /c del @PATH"