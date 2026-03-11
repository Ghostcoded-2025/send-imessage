on run {targetPhone, msg}
	tell application "Messages"
		set targetService to 1st service whose service type is iMessage
		set targetBuddy to buddy targetPhone of targetService
		send msg to targetBuddy
	end tell
end run
