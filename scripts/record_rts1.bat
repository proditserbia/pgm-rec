:: SKRIPTA ZA SNIMANJE RTS1

title RTS1 RECORD [NE GASI]

:: VARIABLES
set path2exe=C:\AutoRec\ffmpeg\bin\ffmpeg.exe
set FSIZE=13
set FCOLOR=black
set BOX=1
set BOXCOLOR=white@0.4
set FFILE='C\:\\Windows\\Fonts\\verdana.ttf'
set VREME=%%d\\\-%%m\\\-%%y %%H\\\:%%M\\\:%%S
set WATERMARK=drawtext="fontsize=%FSIZE%:fontcolor=%FCOLOR%:box=%BOX%:boxcolor=%BOXCOLOR%:fontfile=%FFILE%:text='%%{localtime\:%VREME%}':x=(w-tw)/30:y=(h-th)/20, scale=1024:576, yadif"
set SEGMENTACIJA=stream_segment -segment_time 00:05:00 -segment_atclocktime 1 -reset_timestamps 1 -strftime 1
set IMEFAJLA=%%d%%m%%y-%%H%%M%%S
set STREAM=-v 0 -aspect 16/9 -vcodec mpeg4 -f mpegts udp://127.0.0.1:23001

%path2exe% -f dshow -s 720x576 -framerate 25 -i video="Decklink Video Capture":audio="Decklink Audio Capture" -b:v 1500k -b:a 128k -vf %WATERMARK% -f %SEGMENTACIJA% -c:v libx264 -preset veryfast D:\AutoRec\record\rts1\1_record\%IMEFAJLA%.mp4