# Beyond the Stats- Soccer Predictor

### A data-driven approach to predicting soccer matches.

Beyond the Stats Predictor uses historical soccer data and machine learning to predict the outcome of upcoming matches & full seasons.

Rather than relying solely on league position or basic recent results, the predictor analyzes how teams have performed over time and converts that performance into features that can be used to identify patterns and make predictions on future matchups and full seasons.

## Why I Built It

Beyond the Stats combines two areas I'm interested in: soccer and machine learning. Especially after the website FiveThirtyEight stopped providing their season odds, I wanted to create my own version of their simulations, but with week by week matchup predictions rather than just a seasonlong simulation.

The project goes beyond simply training a model on a dataset. It required building the pipeline that transforms raw historical data into meaningful features, maintaining chronological team statistics, handling multiple seasons, and thousands of individual games across leagues all throughout the world. Each league, season, and even matchup are all unique and different leagues have different factors to take into account, or even different time periods or even fierce rivalry matchups.

## Results

Through initial testing from March 2026-August 2026, Beyond the Stats recorded at **63% prediction** accuracy rate over several different competitions.


Rather than treating prediction accuracy as the only goal, the project provides a framework for experimenting with different features, models, and approaches to determine what information is most useful when predicting soccer. The program also predicts scorelines, goal likelihood, winning odds percentages, and more.

## What It Does

The predictor processes historical match data and generates team-performance statistics such as:

* Recent form
* Goals scored and conceded
* Home and away performance overall & between teams
* Points accumulated over recent matches
* Rolling team statistics
* Historical team performance
* Relative strength between opponents
* Team player values & relative ability

These features are combined to create a large dataset used to train machine-learning models to predict future results.

## The Technical Approach

The project is built in Python using the data science and machine-learning ecosystem.

**Python | Pandas | NumPy | Scikit-learn**

Raw match data is first cleaned and normalized before being processed chronologically. Team statistics are continuously updated as matches are played, allowing each prediction to be based only on information available before that match.

For example, when predicting a match between two teams, the model can use each team's recent performance leading up to that match rather than statistics calculated after the match has already occurred.

This chronological processing is particularly important for avoiding **data leakage** and creating a realistic representation of how the model would perform when making actual predictions.

### Feature Engineering

A significant portion of the project focuses on transforming raw match results into useful predictive features.

Instead of simply providing the model with the final score of previous matches, the system derives statistics that describe the underlying performance of each team.

Features can include rolling averages, recent results, scoring and defensive performance, points, and home/away splits, team scoring efficiency, team game control, etc.

The resulting feature set represents the state of both teams immediately before a match takes place.

### Machine Learning

The processed features are used to train and evaluate machine-learning models through Scikit-learn.

The project separates historical data into training and evaluation sets so that the model can be tested against matches it has not previously seen.

## The Major Challenge

Soccer is an inherently difficult prediction problem with many different factors throughout a single game or season.

Teams change throughout a season, home-field advantage can influence results, and even large differences in team strength do not guarantee a particular outcome always.

Beyond the Stats attempts to account for these factors by continuously updating team statistics and emphasizing recent performance rather than treating an entire season as static, however the program cannot possibly account for all possible outcomes and events throughout a season or match.

## What's Next

Potential improvements coming soon include:

* More advanced machine-learning models
* Player-level statistics & predictions
* Additional leagues and competitions
* More sophisticated team-strength modeling
