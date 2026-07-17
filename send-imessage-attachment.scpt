on run {targetPhone, filePath, serviceType, captionText}
	tell application "Messages"
		if serviceType is "sms" then
			set targetService to 1st service whose service type is SMS
		else
			set targetService to 1st service whose service type is iMessage
		end if
		set targetBuddy to buddy targetPhone of targetService
		if captionText is not "" then
			send captionText to targetBuddy
		end if
		set mediaFile to (POSIX file filePath as alias)
		send mediaFile to targetBuddy
	end tell
end run
