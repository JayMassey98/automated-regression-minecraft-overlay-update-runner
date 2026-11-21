## Print the final totals, release the arena chunk, and report the overall result.

execute store result storage armour:counts passed int 1 run scoreboard players get #passed armour.test
execute store result storage armour:counts failed int 1 run scoreboard players get #failed armour.test
execute store result storage armour:counts skipped int 1 run scoreboard players get #skipped armour.test
function armour:harness/report_counts with storage armour:counts
function armour:harness/cleanup

execute if score #failed armour.test matches 1.. run return run say ARMOUR_RESULT FAILED
say ARMOUR_RESULT PASSED
