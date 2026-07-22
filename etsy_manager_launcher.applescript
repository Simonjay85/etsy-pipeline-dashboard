set dashboardUrl to "http://localhost:8090/"
set projectDir to "/Users/aaronnguyen/Documents/Claude/Projects/Etsy"
set logPath to "/tmp/etsy_dashboard.log"

set isRunning to false
try
	do shell script "lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null"
	set isRunning to true
end try

if not isRunning then
	do shell script "cd " & quoted form of projectDir & " && nohup ./.venv/bin/python dashboard_app.py > " & quoted form of logPath & " 2>&1 &"
	repeat with i from 1 to 20
		delay 0.5
		try
			do shell script "curl -fsS --max-time 1 " & quoted form of dashboardUrl & " >/dev/null"
			exit repeat
		end try
	end repeat
end if

do shell script "open -na 'Google Chrome' --args --app=" & quoted form of dashboardUrl
delay 0.5
tell application "Google Chrome" to activate
