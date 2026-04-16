# File location: /server/tools/openai/logger.py
async def log_iteration(request, iteration, total, status, message):
    # TODO: save everything to db
    print(
        f"REQUEST: {request}, ITERATION: {iteration}, TOTAL ATTEMPTS: {total}, "
        f"STATUS: {status}, MESSAGE: {message}")
