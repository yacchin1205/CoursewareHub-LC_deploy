"""One invalid SSH setting and UI recovery for each on-premise construction recipe."""
from notebook_ui import source


def operate(key, original, run, edit, page, results):
    if key == 'D02':
        setup = next(i for i,c in enumerate(original['cells']) if c['cell_type']=='code' and "hosts_file = './hosts'" in source(c))
        connection = next(i for i,c in enumerate(original['cells']) if c['cell_type']=='code' and "target_group = 'cwhtest0001'" in source(c))
        group = source(original['cells'][connection]).replace("'cwhtest0001'", "'ci_lc4ri'")
        edit(connection, group, 'Select the fixture hosts')
        good = source(original['cells'][setup])
        bad = good.replace("'./hosts'", "'./hosts-bad-user'")
        edit(setup, bad, 'Choose inventory containing an invalid SSH login user')
        run(setup)
    else:
        connection = next(i for i,c in enumerate(original['cells']) if c['cell_type']=='code' and '!ansible -m ping' in source(c))
        setup = connection
        good = source(original['cells'][connection]).replace('cwhtest0001', 'ci_lc4ri').replace("'ci_lc4ri_nodes'", "'ci_lc4ri'")
        if key == 'D01':
            bad = good.replace("'./hosts'", "'./hosts-bad-user'")
        elif key == 'D06':
            good = good.replace('-i ~/ansible/inventory ch-master', '-i ./hosts ci_lc4ri')
            bad = good.replace('-i ./hosts ', '-i ./hosts-bad-user ')
        else:
            good = good.replace('./ci_lc4ri_inventory', './hosts')
            bad = good.replace("'./hosts'", "'./hosts-bad-user'")
        edit(connection, bad, 'Choose inventory containing an invalid SSH login user')
    failed = run(connection, 'RuntimeError')
    assert 'Permission denied' in str(failed['outputs']), failed['outputs']
    page.screenshot(path=str(results / (key + '-invalid-ssh.png')), full_page=True)
    edit(setup, good, 'Correct the SSH inventory through the Notebook UI')
    if setup != connection:
        run(setup)
    recovered = run(connection)
    assert str(recovered['outputs']).count('SUCCESS') >= 2, recovered['outputs']
