## Count the result of the test that just ran.

execute if score #test_skipped armour.test matches 1 run function armour:harness/report_skip with storage armour:run
execute if score #test_skipped armour.test matches 1 run return run scoreboard players add #skipped armour.test 1
execute if score #test_failed armour.test matches 1 run return run scoreboard players add #failed armour.test 1
scoreboard players add #passed armour.test 1