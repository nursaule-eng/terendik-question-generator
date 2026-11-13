from copy import deepcopy
from random import SystemRandom


def shuffle_options(question, rng=None):
    """Return a copy; option text, correct answer and snapshot stay consistent."""
    result = deepcopy(question)
    if result['type'] == 'mcq':
        order = list(range(4))
        (rng or SystemRandom()).shuffle(order)
        result['options'] = [question['options'][i] for i in order]
        result['correct_index'] = order.index(question['correct_index'])
        independent = result.get('validation', {}).get('independent_solution')
        if independent and independent.get('correct_index') in range(4):
            independent['correct_index'] = order.index(independent['correct_index'])
    return result
