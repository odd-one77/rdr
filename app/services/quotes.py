"""Built-in film quotes the mirror can use as its welcome line.

Each entry: id, text, credit (film, year, who). Where the popular wording is a
paraphrase rather than the exact screen line, the credit says so.
"""
from __future__ import annotations

MOVIE_QUOTES: list[dict] = [
    {"id": "bond_expecting", "text": "Ah, Mr. Bond. We've been expecting you.", "credit": "James Bond series (the villains' favourite, paraphrased)"},
    {"id": "bond_name", "text": "Bond. James Bond.", "credit": "Dr. No (1962), Sean Connery"},
    {"id": "bond_shaken", "text": "Shaken, not stirred.", "credit": "Goldfinger (1964), Sean Connery"},
    {"id": "truman", "text": "Good morning! And in case I don't see ya, good afternoon, good evening and good night!", "credit": "The Truman Show (1998), Jim Carrey"},
    {"id": "vietnam", "text": "Good morning, Vietnam!", "credit": "Good Morning, Vietnam (1987), Robin Williams"},
    {"id": "hello_there", "text": "Hello there.", "credit": "Star Wars: Revenge of the Sith (2005), Ewan McGregor"},
    {"id": "force", "text": "May the Force be with you.", "credit": "Star Wars (1977)"},
    {"id": "casablanca", "text": "Here's looking at you, kid.", "credit": "Casablanca (1942), Humphrey Bogart"},
    {"id": "gorgeous", "text": "Hello, gorgeous.", "credit": "Funny Girl (1968), Barbra Streisand"},
    {"id": "clarice", "text": "Good evening, Clarice.", "credit": "The Silence of the Lambs (1991), Anthony Hopkins"},
    {"id": "inigo", "text": "Hello. My name is Inigo Montoya.", "credit": "The Princess Bride (1987), Mandy Patinkin"},
    {"id": "jurassic", "text": "Welcome to Jurassic Park.", "credit": "Jurassic Park (1993), Richard Attenborough"},
    {"id": "diehard_party", "text": "Welcome to the party, pal!", "credit": "Die Hard (1988), Bruce Willis"},
    {"id": "jerry", "text": "You had me at hello.", "credit": "Jerry Maguire (1996), Renée Zellweger"},
    {"id": "terminator", "text": "I'll be back.", "credit": "The Terminator (1984), Arnold Schwarzenegger"},
    {"id": "hasta", "text": "Hasta la vista, baby.", "credit": "Terminator 2: Judgment Day (1991), Arnold Schwarzenegger"},
    {"id": "buzz", "text": "To infinity and beyond!", "credit": "Toy Story (1995), Tim Allen"},
    {"id": "houston", "text": "Houston, we have a problem.", "credit": "Apollo 13 (1995), Tom Hanks"},
    {"id": "coolhand", "text": "What we've got here is failure to communicate.", "credit": "Cool Hand Luke (1967), Strother Martin"},
    {"id": "wargames", "text": "Shall we play a game?", "credit": "WarGames (1983)"},
    {"id": "topgun_speed", "text": "I feel the need... the need for speed!", "credit": "Top Gun (1986), Tom Cruise & Anthony Edwards"},
    {"id": "topgun_goose", "text": "Talk to me, Goose.", "credit": "Top Gun (1986), Tom Cruise"},
    {"id": "jaws", "text": "You're gonna need a bigger boat.", "credit": "Jaws (1975), Roy Scheider"},
    {"id": "billted_excellent", "text": "Be excellent to each other.", "credit": "Bill & Ted's Excellent Adventure (1989)"},
    {"id": "billted_party", "text": "Party on, dudes!", "credit": "Bill & Ted's Excellent Adventure (1989)"},
    {"id": "bttf_roads", "text": "Roads? Where we're going, we don't need roads.", "credit": "Back to the Future (1985), Christopher Lloyd"},
    {"id": "bttf_scott", "text": "Great Scott!", "credit": "Back to the Future (1985), Christopher Lloyd"},
    {"id": "spock", "text": "Live long and prosper.", "credit": "Star Trek, Leonard Nimoy as Spock"},
    {"id": "godfather_offer", "text": "I'm gonna make him an offer he can't refuse.", "credit": "The Godfather (1972), Marlon Brando"},
    {"id": "godfather_close", "text": "Keep your friends close, but your enemies closer.", "credit": "The Godfather Part II (1974), Al Pacino"},
    {"id": "dirty_harry", "text": "Go ahead, make my day.", "credit": "Sudden Impact (1983), Clint Eastwood"},
    {"id": "taxi", "text": "You talkin' to me?", "credit": "Taxi Driver (1976), Robert De Niro"},
    {"id": "shining", "text": "Here's Johnny!", "credit": "The Shining (1980), Jack Nicholson"},
    {"id": "dory", "text": "Just keep swimming.", "credit": "Finding Nemo (2003), Ellen DeGeneres"},
    {"id": "groot", "text": "I am Groot.", "credit": "Guardians of the Galaxy (2014), Vin Diesel"},
    {"id": "wakanda", "text": "Wakanda forever!", "credit": "Black Panther (2018)"},
    {"id": "joker", "text": "Why so serious?", "credit": "The Dark Knight (2008), Heath Ledger"},
    {"id": "titanic", "text": "I'm the king of the world!", "credit": "Titanic (1997), Leonardo DiCaprio"},
    {"id": "gump", "text": "Life is like a box of chocolates.", "credit": "Forrest Gump (1994), Tom Hanks"},
    {"id": "dirty_dancing", "text": "Nobody puts Baby in a corner.", "credit": "Dirty Dancing (1987), Patrick Swayze"},
    {"id": "karate", "text": "Wax on, wax off.", "credit": "The Karate Kid (1984), Pat Morita"},
    {"id": "oz_kansas", "text": "Toto, I've a feeling we're not in Kansas anymore.", "credit": "The Wizard of Oz (1939), Judy Garland"},
    {"id": "oz_home", "text": "There's no place like home.", "credit": "The Wizard of Oz (1939), Judy Garland"},
    {"id": "eve", "text": "Fasten your seatbelts. It's going to be a bumpy night.", "credit": "All About Eve (1950), Bette Davis"},
    {"id": "frankenstein", "text": "It's alive! It's alive!", "credit": "Frankenstein (1931), Colin Clive"},
    {"id": "gone_wind", "text": "After all, tomorrow is another day!", "credit": "Gone with the Wind (1939), Vivien Leigh"},
    {"id": "poets", "text": "Carpe diem. Seize the day.", "credit": "Dead Poets Society (1989), Robin Williams"},
    {"id": "fight_club", "text": "The first rule of Fight Club is: you do not talk about Fight Club.", "credit": "Fight Club (1999), Brad Pitt"},
    {"id": "sherlock", "text": "Elementary, my dear Watson.", "credit": "Sherlock Holmes films (never said quite that way in the books)"},
    {"id": "the_room", "text": "Oh, hi Mark.", "credit": "The Room (2003), Tommy Wiseau"},
]

def enabled(setting: str | None) -> bool:
    return setting == "1"


def selected(setting: str | None) -> list[dict]:
    """The whole list when the Movie quotes box is ticked, otherwise nothing."""
    return list(MOVIE_QUOTES) if enabled(setting) else []
