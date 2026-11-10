from scripts.security_audit import constraint_batches


def test_audit_covers_every_conditional_version_without_duplicates(tmp_path):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text('''# Generated universal pins
numpy==2.4.6 ; python_version < '3.12'
numpy==2.5.4 ; python_version >= '3.12'
Some_Package==1.0 ; sys_platform == 'linux'
some-package==1.0 ; sys_platform == 'win32'
    # via application
''')
    batches = constraint_batches(constraints)
    assert sorted(pin for batch in batches for pin in batch) == [
        "numpy==2.4.6", "numpy==2.5.4", "some-package==1.0"]
    for batch in batches:
        names = [pin.split("==")[0] for pin in batch]
        assert len(names) == len(set(names))
