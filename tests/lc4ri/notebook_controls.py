"""Perform LC4RI cell operations using the visible Classic Notebook controls."""
from types import SimpleNamespace


def operations(page, key, original, records, timeout=120000):
    from notebook_ui import source
    edits = {}
    def find(fragment):
        indices = [i for i, c in enumerate(original['cells']) if c['cell_type'] == 'code' and fragment in source(c)]
        assert len(indices) == 1, (fragment, indices)
        return indices[0]

    def state(index):
        return page.evaluate('''i => {const c=Jupyter.notebook.get_cell(i); return {
            frozen: !!(c.metadata.run_through_control && c.metadata.run_through_control.frozen),
            readOnly: !!c.code_mirror.getOption('readOnly'),
            message: c.last_msg_id || null, count: c.input_prompt_number,
            text: c.get_text(), outputs: c.output_area.outputs};}''', index)

    def select(index):
        cell = page.locator('.cell').nth(index)
        cell.locator('.input_prompt').scroll_into_view_if_needed()
        cell.locator('.input_prompt').click()
        page.keyboard.press('Escape')
        assert page.evaluate('() => Jupyter.notebook.get_selected_index()') == index

    def unfreeze(index):
        select(index)
        if state(index)['frozen']:
            page.locator('button:has(.fa-unfreeze)').click()
            assert not state(index)['frozen']
            records.append(dict(notebook=key, cell=index, action='unfreeze via toolbar'))
        if state(index)['readOnly']:
            page.locator('button:has(.fa-unlock-alt)').click()
            assert not state(index)['readOnly']

    def edit(index, text, reason):
        unfreeze(index)
        before = state(index)['text']
        page.locator('.cell').nth(index).locator('.CodeMirror').click()
        page.keyboard.press('Control+A')
        page.keyboard.insert_text(text)
        assert state(index)['text'] == text
        edits[index] = text
        records.append(dict(notebook=key, cell=index, action='edit working copy',
                            reason=reason, before=before, after=text))

    def run(index, expected_error=None, inputs=()):
        unfreeze(index)
        select(index)
        before = state(index)['message']
        print(f'LC4RI EXEC: {key} cell {index}', flush=True)
        page.keyboard.press('Shift+Enter')
        for answer in inputs:
            prompt = page.locator('.cell').nth(index).locator('input.raw_input')
            prompt.wait_for(state='visible', timeout=60000)
            field = prompt.element_handle()
            prompt.fill(answer)
            prompt.press('Enter')
            field.wait_for_element_state('hidden')
        page.wait_for_function('''({i,before}) => {const c=Jupyter.notebook.get_cell(i);
            return c.last_msg_id && c.last_msg_id !== before && c.input_prompt_number !== '*' &&
                ((c.metadata.run_through_control && c.metadata.run_through_control.frozen) ||
                 c.output_area.outputs.some(o => o.output_type === 'error'));}''',
            arg=dict(i=index, before=before), timeout=timeout)
        observed = state(index)
        errors = [o for o in observed['outputs'] if o.get('output_type') == 'error']
        if expected_error:
            assert len(errors) == 1 and (expected_error is True or errors[0]['ename'] == expected_error), errors
            assert not observed['frozen'], 'Failed cell was frozen as a success'
        else:
            assert not errors, errors
            assert observed['frozen'] and observed['readOnly'], observed
        records.append(dict(notebook=key, cell=index, action='Shift+Enter',
                            frozen=observed['frozen'], expected_error=expected_error,
                            execution_count=observed['count']))
        return observed

    return SimpleNamespace(find=find, state=state, select=select, unfreeze=unfreeze,
                           edit=edit, run=run, edits=edits)
