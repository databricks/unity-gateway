Run `python slow_job.py` as a background task. It prints a line `TOKEN=<value>` right away, then waits until you write that value to `ack.txt`. After the ack it runs for about a minute and a half more, then prints a line `RESULT=<value>`.

Write the result value to `result.txt`. The token and result are only printed to the job's output. This session ends when you reply, which stops any background job, so keep checking the job's output until it prints the result before you reply.
