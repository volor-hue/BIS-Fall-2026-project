# payments/qr.py
# ------------------------------------------------------------
# A tiny QR code generator (pure Python, no extra packages, no CDN) for the Kaspi QR demo.
# QR "byte" mode, error correction level M, versions 1..10 (up to ~213 bytes of text).
# Result: an SVG picture (black squares on white) the page shows as is.
# Algorithm: ISO/IEC 18004 (finder/timing/alignment patterns, Reed-Solomon, zigzag, mask).
# ------------------------------------------------------------

# Level M, versions 0..10 (index 0 unused): error-correction bytes per block and number of blocks.
_ECC_PER_BLOCK = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
_FORMAT_M = 0  # 2-bit code of level M in the format information


def _raw_modules(ver):
    """How many modules (bits) of a version are left for data + error correction."""
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_capacity(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x, y):
    # Multiplication in GF(2^8) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1.
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    result = [6]
    pos = size - 7
    for _ in range(n - 1):
        result.insert(1, pos)
        pos -= step
    return result


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark):
        self.mod[y][x] = dark
        self.func[y][x] = True

    def draw_function_patterns(self):
        s = self.size
        for i in range(s):                       # timing patterns
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):   # finder patterns + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):              # alignment patterns
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)                      # reserve the area (real bits come after masking)
        if self.ver >= 7:                        # version information
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def draw_format(self, mask):
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        s = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, s - 15 + i, bit(i))
        self.set(8, s - 8, True)                 # the "dark module"

    def place_data(self, codewords):
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.func[y][x] and i < len(codewords) * 8:
                        self.mod[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and test(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self):
        # Simplified penalty (rules 1, 2 and 4 of the standard): long runs, 2x2 blocks, dark balance.
        s, m, score = self.size, self.mod, 0
        for lines in (m, [list(col) for col in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    run = run + 1 if v == prev else 1
                    prev = v
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        score += abs(dark * 20 - s * s * 10) // (s * s) * 10
        return score


def _codewords(text_bytes, ver):
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)                                   # byte mode
    put(len(text_bytes), 8 if ver <= 9 else 16)      # character count
    for b in text_bytes:
        put(b, 8)
    capacity = _data_capacity(ver) * 8
    put(0, min(4, capacity - len(bits)))             # terminator
    put(0, (8 - len(bits) % 8) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11

    # Split into blocks, add Reed-Solomon bytes, interleave.
    num_blocks, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    num_short = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(num_blocks):
        length = short_len - ecc_len + (0 if i < num_short else 1)
        block = data[k:k + length]
        k += length
        ecc = _rs_remainder(block, divisor)
        if i < num_short:
            block = block + [0]
        blocks.append(block + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                result.append(block[i])
    return result


def matrix(text):
    """QR code of `text` as a list of rows of True (dark) / False (light)."""
    data = text.encode("utf-8")
    for ver in range(1, 11):
        header_bits = 4 + (8 if ver <= 9 else 16)
        if header_bits + len(data) * 8 <= _data_capacity(ver) * 8:
            break
    else:
        raise ValueError("Text is too long for this small QR generator")
    codewords = _codewords(data, ver)
    best = None
    for mask in range(8):
        qr = _Matrix(ver)
        qr.draw_function_patterns()
        qr.place_data(codewords)
        qr.apply_mask(mask)
        qr.draw_format(mask)
        score = qr.penalty()
        if best is None or score < best[0]:
            best = (score, qr)
    return best[1].mod


def svg(text, module=6, border=4):
    """QR code of `text` as an SVG string (one <path>, so it stays small)."""
    rows = matrix(text)
    size = len(rows) + 2 * border
    parts = [f"M{x + border},{y + border}h1v1h-1z"
             for y, row in enumerate(rows) for x, dark in enumerate(row) if dark]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size * module}" height="{size * module}" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR code">'
            f'<rect width="100%" height="100%" fill="#fff"/><path d="{"".join(parts)}" fill="#000"/></svg>')
