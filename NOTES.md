There's still the matter of `A AND B AND C` is treated differently than `A AND C AND B`, I wonder if we can order the arguments of N-ary operators, maybe it makes sense to accumulate operands in a set and order them in a reproducible way when we need to convert to something else?

Second I have found in the past it's very convienent to implement the & and | operators so that I can do `node_a & node_b` or `node_c | node_d`

Right now in sql generation we compile right down to a string, but I'd prefer to compile down to

1. a string with parameterized sql
2. the parameters to send along with the query

Black - 💀
Blue - 💧
Green - 🌳
Red - 🔥
White - ☀️
