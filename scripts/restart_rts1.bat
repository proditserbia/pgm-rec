:: RESTART RTS1

taskkill /F /FI "WINDOWTITLE eq RTS1 PREVIEW [NE GASI]" /T
taskkill /F /FI "WINDOWTITLE eq RTS1 RECORD [NE GASI]" /T
timeout /t 5
start /min C:\AutoRec\Scripts\record\record_rts1.bat & start /min C:\AutoRec\Scripts\preview\rts1_preview.bat & exit