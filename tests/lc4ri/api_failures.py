"""Check an unavailable AWS endpoint and recovery using each original first API cell."""
def operate(key, original, run, page, records, results, server):
    from notebook_ui import source
    index=next(i for i,c in enumerate(original['cells']) if c['cell_type']=='code' and '!aws ec2 describe-vpcs' in source(c))
    server.stop()
    try:
        failed=run(index, True)
        assert 'Could not connect to the endpoint URL' in str(failed['outputs']), failed['outputs']
        page.screenshot(path=str(results/(key+'-unavailable-api.png')),full_page=True)
        records.append(dict(notebook=key,action='observed unavailable AWS API endpoint'))
    finally:
        server.start()
    recovered=run(index)
    assert 'VPCs exist' in str(recovered['outputs']), recovered['outputs']
    records.append(dict(notebook=key,action='restored AWS API and reran the original cell through the UI'))
