on run {targetPhone, msg, serviceType}
	tell application "Messages"
		if serviceType is "sms" then
			set targetService to 1st service whose service type is SMS
		else
			set targetService to 1st service whose service type is iMessage
		end if
		set targetBuddy to buddy targetPhone of targetService
		send msg to targetBuddy
	end tell
end run
