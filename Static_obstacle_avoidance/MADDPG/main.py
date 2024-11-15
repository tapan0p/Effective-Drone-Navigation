

import matplotlib.pyplot as plt
import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torchsummary import summary
from arguments import parse_args
from replay_buffer import ReplayBuffer
from model import MLPActor, MLPQFunction
from Static_obstacle_avoidance.ApfAlgorithm import APF
from Static_obstacle_avoidance.Method import getReward, setup_seed
from Static_obstacle_avoidance.draw import Painter


def get_trainers(numberOfAgents, obs_shape_n, action_shape_n, arglist):
    actor_cur = [None for _ in range(numberOfAgents)]
    critics_cur = [None for _ in range(numberOfAgents)]
    actor_tar = [None for _ in range(numberOfAgents)]
    critics_tar = [None for _ in range(numberOfAgents)]
    optimizers_c = [None for _ in range(numberOfAgents)]
    optimizers_a = [None for _ in range(numberOfAgents)]

    for i in range(numberOfAgents):
        actor_cur[i] = MLPActor(obs_shape_n[i], action_shape_n[i], [arglist.action_limit_min, arglist.action_limit_max]).to(arglist.device)
        critics_cur[i] = MLPQFunction(sum(obs_shape_n), sum(action_shape_n)).to(arglist.device)
        actor_tar[i] = MLPActor(obs_shape_n[i], action_shape_n[i], [arglist.action_limit_min, arglist.action_limit_max]).to(arglist.device)
        critics_tar[i] = MLPQFunction(sum(obs_shape_n), sum(action_shape_n)).to(arglist.device)
        optimizers_a[i] = optim.Adam(actor_cur[i].parameters(), arglist.lr_a)
        optimizers_c[i] = optim.Adam(critics_cur[i].parameters(), arglist.lr_c)
    actor_tar = update_trainers(actor_cur, actor_tar, 1.0)  # just copy it
    critics_tar = update_trainers(critics_cur, critics_tar, 1.0)
    return actor_cur, critics_cur, actor_tar, critics_tar, optimizers_a, optimizers_c

def update_trainers(agents_cur, agents_tar, tao):
    for agent_c, agent_t in zip(agents_cur, agents_tar):
        key_list = list(agent_c.state_dict().keys())
        state_dict_t = agent_t.state_dict()
        state_dict_c = agent_c.state_dict()
        for key in key_list:
            state_dict_t[key] = state_dict_c[key]*tao + \
                    (1-tao)*state_dict_t[key]
        agent_t.load_state_dict(state_dict_t)
    return agents_tar

def agents_train(arglist, game_step, update_cnt, memory, obs_size, action_size,
                 actors_cur, actors_tar, critics_cur, critics_tar, optimizers_a, optimizers_c):
    if game_step > arglist.learning_start_step and \
        (game_step - arglist.learning_start_step) % arglist.learning_fre == 0:
        if update_cnt == 0: print('\r=start training ...')
        update_cnt += 1

        for agent_idx, (actor_c, actor_t, critic_c, critic_t, opt_a, opt_c) in \
            enumerate(zip(actors_cur, actors_tar, critics_cur, critics_tar, optimizers_a, optimizers_c)):

            _obs_n_o, _action_n, _rew_n, _obs_n_n, _done_n = memory.sample(
                arglist.batch_size, agent_idx) 
            rew = torch.tensor(_rew_n, device=arglist.device, dtype=torch.float)
            done_n = torch.tensor(_done_n, device=arglist.device, dtype=torch.float)
            action_cur_o = torch.from_numpy(_action_n).to(arglist.device, torch.float)
            obs_n_o = torch.from_numpy(_obs_n_o).to(arglist.device, torch.float)
            obs_n_n = torch.from_numpy(_obs_n_n).to(arglist.device, torch.float)
            action_tar = torch.cat([a_t(obs_n_n[:, obs_size[idx][0]:obs_size[idx][1]]).detach() \
                                    for idx, a_t in enumerate(actors_tar)], dim=1)
            q = critic_c(obs_n_o, action_cur_o).reshape(-1)  
            with torch.no_grad():
                q_ = critic_t(obs_n_n, action_tar).reshape(-1)  
                tar_value = q_ * arglist.gamma * (1 - done_n) + rew  
            loss_c = torch.nn.MSELoss()(q, tar_value)  
            opt_c.zero_grad()
            loss_c.backward()
            opt_c.step()

            # --use the data to update the ACTOR
            # There is no need to cal other agent's action
            policy_c_new = actor_c(obs_n_o[:, obs_size[agent_idx][0]:obs_size[agent_idx][1]])
            # update the aciton of this agent
            action_cur_o[:, action_size[agent_idx][0]:action_size[agent_idx][1]] = policy_c_new
            loss_a = torch.mul(-1, torch.mean(critic_c(obs_n_o, action_cur_o)))

            opt_a.zero_grad()
            loss_a.backward()
            # nn.utils.clip_grad_norm_(actor_c.parameters(), arglist.max_grad_norm)
            opt_a.step()

        actors_tar = update_trainers(actors_cur, actors_tar, arglist.tao)
        critics_tar = update_trainers(critics_cur, critics_tar, arglist.tao)
    return update_cnt, actors_cur, actors_tar, critics_cur, critics_tar


def train(arglist):
    apf = APF()
    
    obs_shape_n = [6 for i in range(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone)] 
    action_shape_n = [1 for i in range(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone)]
    actors_cur, critics_cur, actors_tar, critics_tar, optimizers_a, optimizers_c = \
        get_trainers(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone, obs_shape_n, action_shape_n, arglist)
    memory = ReplayBuffer(arglist.memory_size)
    """step3: init the pars"""
    obs_size = [] 
    action_size = [] 
    head_o, head_a, end_o, end_a = 0, 0, 0, 0
    for obs_shape, action_shape in zip(obs_shape_n, action_shape_n):
        end_o = end_o + obs_shape
        end_a = end_a + action_shape
        range_o = (head_o, end_o)
        range_a = (head_a, end_a)
        obs_size.append(range_o)
        action_size.append(range_a)
        head_o = end_o
        head_a = end_a

    print('=3 starting iterations ...')
    rewardList = []
    maxReward = -np.inf
    var = 3
    game_step = 0
    update_cnt = 0
    for episode_gone in range(arglist.max_episode):
        q = apf.x0
        apf.reset()
        rewardSum = 0
        pathLength = 0
        qBefore = [None, None, None] 
        for episode_cnt in range(arglist.per_episode_max_len):
            obsDicq = apf.calculateDynamicState(q) 
            obs_n_sphere, obs_n_cylinder, obs_n_cone = obsDicq['sphere'], obsDicq['cylinder'], obsDicq['cone']
            obs_n = obs_n_sphere + obs_n_cylinder + obs_n_cone
            # get action
            if episode_gone > arglist.actor_begin_work:
                if episode_gone == arglist.actor_begin_work + 1 and episode_cnt == 0: print('==actor begin to work.')
                if var <= 0.10: var = 0.10 
                else: var *= 0.9999
                action_n = [agent(torch.from_numpy(obs).to(arglist.device, torch.float)).detach().cpu().numpy()[0]\
                            for agent, obs in zip(actors_cur, obs_n)]                           
                action_n = np.clip(np.random.normal(action_n, var), arglist.action_limit_min, arglist.action_limit_max)
                action_sphere = action_n[0:apf.numberOfSphere]
                action_cylinder = action_n[apf.numberOfSphere: apf.numberOfSphere + apf.numberOfCylinder]
                action_cone = action_n[apf.numberOfSphere + apf.numberOfCylinder: apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone]
            else: 
                if episode_gone == 0 and episode_cnt == 0: print('==this period of time the actions will be sampled randomly')
                action_n = [random.uniform(arglist.action_limit_min, arglist.action_limit_max) \
                            for _ in range(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone)]
                action_sphere = action_n[0:apf.numberOfSphere]
                action_cylinder = action_n[apf.numberOfSphere:apf.numberOfSphere + apf.numberOfCylinder]
                action_cone = action_n[apf.numberOfSphere + apf.numberOfCylinder: apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone]

            # interact with enviroment
            qNext = apf.getqNext(apf.epsilon0, action_sphere, action_cylinder, action_cone, q, qBefore)

            flag = apf.checkCollision(qNext)
            obsDicqNext = apf.calculateDynamicState(qNext)
            new_obs_n_sphere, new_obs_n_cylinder, new_obs_n_cone = obsDicqNext['sphere'], obsDicqNext['cylinder'], obsDicqNext['cone']
            new_obs_n = new_obs_n_sphere + new_obs_n_cylinder + new_obs_n_cone
            done_n = [True if apf.distanceCost(apf.qgoal, qNext) < apf.threshold else False\
                    for _ in range(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone)]
            pathLength += apf.distanceCost(q, qNext)

            rew_n = [getReward(flag, apf, qBefore, q, qNext) for _ in range(apf.numberOfSphere + apf.numberOfCylinder + apf.numberOfCone)] 
            rewardSum += rew_n[0]
            # save the experience
            memory.add(obs_n, action_n, rew_n, new_obs_n, done_n)

            # train our agents
            update_cnt, actors_cur, actors_tar, critics_cur, critics_tar = agents_train(
                arglist, game_step, update_cnt, memory, obs_size, action_size,
                actors_cur, actors_tar, critics_cur, critics_tar, optimizers_a, optimizers_c)
            # update the position
            game_step += 1
            qBefore = q
            q = qNext
            if all(done_n): 
                pathLength += apf.distanceCost(q, apf.qgoal)
                break
        print('Episode:', episode_gone, 'Reward:%f' % rewardSum, 'path length:%f' % pathLength, 'var:%f' % var, 'update_cnt:%d' % update_cnt)
        rewardList.append(round(rewardSum,2))
        if episode_gone > arglist.max_episode*2/3:
            if rewardSum > maxReward:
                maxReward = rewardSum
                for idx, pi in enumerate(actors_cur):
                    if idx < apf.numberOfSphere:
                        torch.save(pi, 'TrainedModel/Actor1.%d.pkl'%idx)
                    else:
                        if idx < apf.numberOfSphere + apf.numberOfCylinder:
                            torch.save(pi, 'TrainedModel/Actor2.%d.pkl'%(idx - apf.numberOfSphere))
                        else:
                            torch.save(pi, 'TrainedModel/Actor3.%d.pkl' % (idx - apf.numberOfSphere - apf.numberOfCylinder))

    
    painter = Painter(load_csv=True,load_dir='F:/MasterDegree/figure_data_5.csv')
    painter.addData(rewardList,'MADDPG',smooth=True)
    painter.saveData('F:/MasterDegree/figure_data_5.csv')
    painter.drawFigure()



if __name__ == '__main__':
    setup_seed(12)
    arglist = parse_args()
    train(arglist)
