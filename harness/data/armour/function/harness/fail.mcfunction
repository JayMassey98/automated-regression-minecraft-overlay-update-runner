## Record a failed assertion for the current test. Call with {message:"what went wrong"}.
## Keep messages free of double quotes and @ selectors.

$data modify storage armour:run message set value "$(message)"
scoreboard players set #test_failed armour.test 1
function armour:harness/report_fail with storage armour:run