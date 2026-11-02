"""Unit tests for the 29 game logic (trick winner, follow-suit, rounds)."""
import unittest

from server import Card, Game, card_beats


def make_game(trump='spades'):
    game = Game('TEST01')
    for seat in range(4):
        game.players[seat] = {
            'ws': None, 'name': f'P{seat}', 'token': 'tok', 'connected': True,
        }
    game.host_seat = 0
    game.state = 'playing'
    game.trump_suit = trump
    return game


def card(suit, rank):
    return Card(suit, rank)


class CardBeatsTests(unittest.TestCase):
    def test_higher_same_suit_wins(self):
        self.assertTrue(card_beats(card('hearts', 'A'), card('hearts', 'K'), 'spades'))
        self.assertFalse(card_beats(card('hearts', 'K'), card('hearts', 'A'), 'spades'))

    def test_trump_beats_led_suit(self):
        self.assertTrue(card_beats(card('spades', '7'), card('hearts', 'A'), 'spades'))

    def test_led_suit_never_beats_trump(self):
        self.assertFalse(card_beats(card('hearts', 'A'), card('spades', '7'), 'spades'))

    def test_off_suit_never_beats_led_suit(self):
        self.assertFalse(card_beats(card('diamonds', 'A'), card('hearts', '7'), 'spades'))


class TrickWinnerTests(unittest.TestCase):
    def resolve(self, plays, trump='spades'):
        game = make_game(trump)
        game.current_trick = [(seat, card(suit, rank)) for seat, suit, rank in plays]
        winner, points = game.complete_trick()
        return winner, points

    def test_trump_beats_later_led_suit_card(self):
        # Regression: a higher led-suit card played after a trump must NOT
        # overtake the trump.
        winner, _ = self.resolve([
            (0, 'hearts', 'A'),
            (1, 'spades', 'J'),
            (2, 'hearts', 'K'),
        ])
        self.assertEqual(winner, 1)

    def test_highest_led_suit_wins_without_trump(self):
        winner, _ = self.resolve([
            (0, 'hearts', '7'),
            (1, 'hearts', 'K'),
            (2, 'hearts', '9'),
            (3, 'clubs', 'A'),
        ])
        self.assertEqual(winner, 1)

    def test_trump_vs_trump_higher_wins(self):
        winner, _ = self.resolve([
            (0, 'spades', '9'),
            (1, 'spades', 'A'),
            (2, 'spades', 'K'),
            (3, 'diamonds', 'A'),
        ])
        self.assertEqual(winner, 1)

    def test_points_are_summed(self):
        _, points = self.resolve([
            (0, 'hearts', 'A'),   # 11
            (1, 'clubs', 'K'),    # 4
            (2, 'diamonds', '10'),  # 10
            (3, 'spades', 'J'),   # 2  (trump, wins)
        ])
        self.assertEqual(points, 27)

    def test_trick_points_go_to_winner_team(self):
        game = make_game()
        game.current_trick = [
            (0, card('hearts', 'A')),    # 11
            (1, card('clubs', 'K')),     # 4
            (2, card('diamonds', '10')),  # 10
            (3, card('spades', 'J')),    # 2, trump -> seat 3 -> team B
        ]
        winner, _ = game.complete_trick()
        self.assertEqual(winner, 3)
        self.assertEqual(game.teams['B']['score'], 27)
        self.assertEqual(game.teams['B']['cards'], 4)
        self.assertEqual(game.teams['A']['score'], 0)


class PlayCardTests(unittest.TestCase):
    def test_not_your_turn(self):
        game = make_game()
        game.hands = {s: [card('hearts', str(r)) for r in ('7', '8', '9')] for s in range(4)}
        game.current_player = 2
        result = game.play_card(1, 0)
        self.assertIn('error', result)

    def test_follow_suit_enforced(self):
        game = make_game()
        game.hands = {
            0: [card('spades', 'A'), card('hearts', '7')],
            1: [card('clubs', 'K')],
            2: [card('diamonds', 'K')],
            3: [card('clubs', 'Q')],
        }
        game.current_player = 0
        game.current_trick = [(1, card('hearts', 'K'))]  # hearts was led

        # Holding a heart, playing spades is illegal.
        result = game.play_card(0, 0)
        self.assertIn('error', result)

        # Playing the heart is fine.
        result = game.play_card(0, 1)
        self.assertNotIn('error', result)
        self.assertEqual(result['card']['suit'], 'hearts')

    def test_may_dump_when_void(self):
        game = make_game()
        game.hands = {
            0: [card('spades', 'A')],
            1: [card('clubs', 'K')],
            2: [card('diamonds', 'K')],
            3: [card('clubs', 'Q')],
        }
        game.current_player = 0
        game.current_trick = [(1, card('hearts', 'K'))]
        result = game.play_card(0, 0)
        self.assertNotIn('error', result)

    def test_trick_completes_and_pauses(self):
        game = make_game()
        game.hands = {
            0: [card('hearts', '7'), card('clubs', '9')],
            1: [card('hearts', '8'), card('clubs', '10')],
            2: [card('hearts', '9'), card('diamonds', '7')],
            3: [card('spades', '7'), card('diamonds', '8')],
        }
        game.current_player = 0
        for seat in range(4):
            result = game.play_card(seat, 0)
            self.assertNotIn('error', result, msg=str(result))
        self.assertIsNotNone(result['trickComplete'])
        self.assertEqual(result['trickComplete']['winnerSeat'], 3)
        self.assertEqual(game.state, 'playing')  # cards left, round continues
        self.assertIsNone(game.current_player)  # paused between tricks
        self.assertTrue(game.begin_next_trick(3))
        self.assertEqual(game.current_player, 3)
        self.assertEqual(game.current_trick, [])

    def test_invalid_index(self):
        game = make_game()
        game.hands = {s: [card('hearts', '7')] for s in range(4)}
        game.current_player = 0
        self.assertIn('error', game.play_card(0, 5))
        self.assertIn('error', game.play_card(0, -1))
        self.assertIn('error', game.play_card(0, 'x'))


class RoundLifecycleTests(unittest.TestCase):
    def test_start_game_resets_scores(self):
        game = make_game()
        game.teams['A']['score'] = 45
        game.teams['A']['cards'] = 16
        game.state = 'finished'
        game.game_over = {'winningTeam': 'A', 'teams': game.teams}

        self.assertTrue(game.start_game())
        self.assertEqual(game.state, 'playing')
        self.assertEqual(game.teams['A']['score'], 0)
        self.assertEqual(game.teams['A']['cards'], 0)
        self.assertIsNone(game.game_over)
        self.assertEqual(sum(len(h) for h in game.hands.values()), 32)
        self.assertIn(game.trump_suit, Card.SUITS)
        self.assertIsNotNone(game.current_player)

    def test_start_game_needs_four_players(self):
        game = Game('TEST02')
        game.add_player(None, 'P0')
        self.assertFalse(game.start_game())

    def test_final_trick_ends_round_with_winner(self):
        game = make_game()
        # One card left each; team A (seats 0,2) takes the trick.
        game.hands = {
            0: [card('hearts', 'A')],
            1: [card('hearts', '7')],
            2: [card('hearts', '8')],
            3: [card('clubs', '9')],
        }
        game.current_player = 0
        for seat in range(4):
            result = game.play_card(seat, 0)
            self.assertNotIn('error', result, msg=str(result))

        self.assertEqual(game.state, 'finished')
        self.assertIsNotNone(game.game_over)
        self.assertEqual(game.game_over['winningTeam'], 'A')
        # Team A won 4/4 tricks and gets the +29 bonus on top of card points
        # (only the Ace scores: 11, the 7/8/9 are worth 0).
        self.assertEqual(game.teams['A']['cards'], 4)
        self.assertEqual(game.teams['A']['score'], 11 + 29)
        self.assertIsNone(game.current_player)
        # A finished game rejects further plays.
        self.assertIn('error', game.play_card(0, 0))

    def test_cancel_returns_to_waiting(self):
        game = make_game()
        game.start_game()
        game.cancel_game()
        self.assertEqual(game.state, 'waiting')
        self.assertEqual(game.hands, {})
        self.assertIsNone(game.trump_suit)
        self.assertIsNone(game.game_over)
        self.assertEqual(game.teams['A']['score'], 0)


if __name__ == '__main__':
    unittest.main()
