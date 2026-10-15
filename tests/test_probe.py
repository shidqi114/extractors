from extractor.probe import probe


class FakeSSH:
    def __init__(self):
        self.commands = []

    def run(self, command, timeout=90):
        self.commands.append(command)
        if "-name 20220324111256_83rxz.jpg" in command:
            return "/var/www/hous/foto/20220324111256_83rxz.jpg\n"
        if "ss -ltn" in command:
            return "LISTEN 0 200 127.0.0.1:3306 0.0.0.0:*\n"
        return ""


def test_probe_finds_file_across_mounts_and_suggests_dir():
    ssh = FakeSSH()
    result = probe(ssh, "20220324111256_83rxz.jpg")
    assert result["engine_guess"] == "mysql"
    assert result["suggested_sftp_dirs"] == ["/var/www/hous/foto"]
    file_cmd = next(c for c in ssh.commands if "20220324111256_83rxz.jpg" in c)
    assert "-xdev" not in file_cmd


def test_probe_falls_back_to_similar_names():
    class NoExact(FakeSSH):
        def run(self, command, timeout=90):
            self.commands.append(command)
            return "/data/img/20220324111256_83rxz_thumb.jpg\n" if "-iname '*83rxz*'" in command else ""

    result = probe(NoExact(), "20220324111256_83rxz.jpg")
    assert result["similar_files"] == ["/data/img/20220324111256_83rxz_thumb.jpg"]
