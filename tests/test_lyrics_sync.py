import math
import pytest

from prosperos_hoard import lyrics_sync as sync


def heard(words):
    return {'segments': [{'words': [{'word': word, 'start_s': start, 'end_s': end}
                                    for word, start, end in words]}]}


def test_repeated_chorus_uses_each_occurrence_and_preserves_supplied_text():
    text = '[Chorus]\nBlow up the night!\n[Verse]\nA quiet street.\n[Chorus]\nBlow up the night!'
    transcript = heard([(w, at + i * .3, at + i * .3 + .2)
                        for at, phrase in [(3, 'blow up the night'), (12, 'a quiet street'), (30, 'blow up the night')]
                        for i, w in enumerate(phrase.split())])
    out = sync.align_words(text, transcript, 40)
    assert out['complete']
    assert [row['time_s'] for row in out['lines']] == [3, 12, 30]
    assert [row['text'] for row in out['lines']] == ['Blow up the night!', 'A quiet street.', 'Blow up the night!']
    assert out['lines'][2]['end_s'] < 32  # retain the instrumental gap after the final sung word


def test_unheard_line_is_retained_without_fabricated_timestamps():
    out = sync.align_words('A quiet street\nSomething entirely missing\nBright blue lights',
        heard([(w, i, i + .5) for i, w in enumerate('a quiet street bright blue lights'.split())]), 10)
    assert out['timed_lines'] == 2 and not out['complete']
    assert out['lines'][1]['text'] == 'Something entirely missing'
    assert out['lines'][1]['time_s'] is None and out['lines'][1]['end_s'] is None


def test_imperfect_first_chorus_does_not_take_the_cleaner_second_occurrence():
    chorus = ['Silver lights fill the sky', 'Rising sparks reflect the glow', 'Dancers turn beneath the stars']
    bridge = 'Quiet streets surround an empty theater tonight'
    text = '\n'.join([*chorus, bridge, *chorus])
    phrases = [(3, chorus[0]), (6, 'rising spark reflects a glow'),
               (9, 'dancers turn under some stars'), (15, bridge), (25, 'unexpected applause'),
               (30, chorus[0]), (33, chorus[1]), (36, chorus[2])]
    transcript = heard([(word, at + i * .25, at + i * .25 + .2)
                        for at, phrase in phrases for i, word in enumerate(phrase.lower().split())])
    rows = sync.align_words(text, transcript, 40)['lines']
    assert rows[0]['time_s'] == 3 and rows[3]['time_s'] == 15
    assert [row['time_s'] for row in rows[4:]] == [30, 33, 36]
    assert rows[1]['time_s'] is None  # uncertain words stay available for manual timing


def test_matching_words_separated_by_a_long_gap_never_form_one_verse():
    words = [('silver', 3, 3.2), ('lights', 3.2, 3.5), ('fill', 4, 4.2),
             ('the', 65, 65.2), ('sky', 65.2, 65.5)]
    row = sync.align_words('Silver lights fill the sky', heard(words), 70)['lines'][0]
    assert row['time_s'] is None and row['end_s'] is None and row['needs_review']


def test_empty_or_invalid_word_times_never_become_alignment():
    for transcript in ({}, heard([('hello', math.nan, 2), ('world', 3, 2)])):
        assert sync.align_words('Hello world', transcript, 5)['timed_lines'] == 0


def test_human_times_keep_gaps_and_untimed_lines_and_sections():
    rows = sync.normalize_timings('[Verse]\nOne\nTwo\nThree', [
        {'text': 'One', 'time_s': 1.125, 'end_s': 2.25},
        {'text': 'Two', 'time_s': None, 'end_s': None},
        {'text': 'Three', 'time_s': 5, 'end_s': 6}], 10)
    assert rows[0]['end_s'] == 2.25 and rows[2]['time_s'] == 5
    assert rows[1]['time_s'] is None
    assert sync.as_lrc(rows) == '[00:01.12][Verse]\n[00:01.12]One\n[00:05.00]Three'


@pytest.mark.parametrize('row', [
    {'text': 'Changed', 'time_s': 1, 'end_s': 2},
    {'text': 'One', 'time_s': 1, 'end_s': None},
    {'text': 'One', 'time_s': -1, 'end_s': 2},
    {'text': 'One', 'time_s': 2, 'end_s': 1},
    {'text': 'One', 'time_s': 1, 'end_s': math.inf},
    {'text': 'One', 'time_s': 1, 'end_s': 11},
])
def test_invalid_human_times_or_stale_text_are_reported(row):
    with pytest.raises(ValueError):
        sync.normalize_timings('One', [row], 10)


def test_overlaps_and_omitted_lines_are_reported():
    with pytest.raises(ValueError, match='overlaps'):
        sync.normalize_timings('One\nTwo', [{'text':'One','time_s':1,'end_s':3},
            {'text':'Two','time_s':2,'end_s':4}], 10)
    with pytest.raises(ValueError, match='every line'):
        sync.normalize_timings('One\nTwo', [], 10)
